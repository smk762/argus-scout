from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from prometheus_client.parser import text_string_to_metric_families

from argus_scout.budget import DailyBudget
from argus_scout.config import parse_config
from argus_scout.events import StatusEvent
from argus_scout.recommendations import Recommendation, Status
from argus_scout.server.app import create_app
from argus_scout.severity import Severity
from argus_scout.sources import Sources
from argus_scout.store import SqliteStore

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
NOW = datetime(2026, 6, 1, 16, 30, tzinfo=UTC)  # 16:30 UTC: the example tariff's peak period


def rec(n: int, status: Status = Status.PROPOSED) -> Recommendation:
    return Recommendation(
        title=f"rec {n}", action="do it", impact="low", analyzer="power", severity=Severity.NOTICE, status=status
    )


@pytest.fixture
def store():
    with SqliteStore(":memory:") as s:
        for i, sev in enumerate(["info", "warning", "warning", "critical"]):
            s.upsert_event(
                StatusEvent.from_dict({"event_id": f"e{i}", "source": "nas", "severity": sev, "summary": f"s{i}"}),
                now=NOW,
            )
        s.ack_event("nas", "e0", "me", at=NOW)  # acked: not open
        expired = {"event_id": "old", "source": "nas", "severity": "critical", "summary": "gone"}
        s.upsert_event(StatusEvent.from_dict({**expired, "expires_at": (NOW - timedelta(hours=1)).isoformat()}))

        manual, standing, rejected = rec(1), rec(2), rec(3)
        for r in (manual, standing, rejected, rec(4)):
            s.save_recommendation(r)
        manual.approve()
        standing.approve(rule_id="low-impact")
        rejected.reject("no")
        for r in (manual, standing, rejected):
            s.save_recommendation(r)

        s.record_job_run("status", started_at=NOW, finished_at=NOW + timedelta(seconds=2))
        s.record_job_run("status", started_at=NOW, finished_at=NOW, error="RuntimeError: boom")
        yield s


def scrape(app) -> dict[str, dict[tuple, float]]:
    with TestClient(app) as client:
        response = client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    families: dict[str, dict[tuple, float]] = {}
    for family in text_string_to_metric_families(response.text):
        for sample in family.samples:
            families.setdefault(sample.name, {})[tuple(sorted(sample.labels.items()))] = sample.value
    return families


def make_app(store, tmp_path, *, tariff: Path | None = EXAMPLES / "tariff.yaml"):
    config = parse_config(
        {"files": {"tariff": str(tariff) if tariff else None}, "schedule": {"timezone": "UTC"}}, tmp_path
    )
    budget = DailyBudget({"llm": 10, "tts": 0}, today=lambda: date(2026, 6, 1))
    for _ in range(3):
        budget.consume("llm")
    budget.consume("speak")
    return create_app(config, store=store, budget=budget, sources=Sources(), clock=lambda: NOW, run_scheduler=False)


def test_metrics_families_from_a_seeded_store(store, tmp_path):
    m = scrape(make_app(store, tmp_path))

    assert m["scout_usage_used"] == {(("kind", "llm"),): 3, (("kind", "speak"),): 1, (("kind", "tts"),): 0}
    assert m["scout_usage_limit"] == {(("kind", "llm"),): 10, (("kind", "speak"),): 0, (("kind", "tts"),): 0}

    assert m["scout_tariff_loaded"] == {(): 1}
    assert m["scout_tariff_period"] == {(("period", "peak"), ("plan", "time_of_use")): 1}
    rate_labels = (("currency", "USD"), ("period", "peak"), ("plan", "time_of_use"), ("unit", "kWh"))
    assert m["scout_tariff_rate"] == {rate_labels: 48.0}
    assert m["scout_tariff_next_change_seconds"] == {(("next_period", "off_peak"),): 4.5 * 3600}

    assert m["scout_recommendations"] == {
        (("status", "draft"),): 0,
        (("status", "proposed"),): 1,
        (("status", "approved"),): 2,
        (("status", "rejected"),): 1,
        (("status", "deployed"),): 0,
        (("status", "retired"),): 0,
    }
    assert m["scout_approvals_total"] == {(("source", "manual"),): 1, (("source", "standing"),): 1}
    assert m["scout_inbox_open_events"] == {
        (("severity", "info"),): 0,
        (("severity", "notice"),): 0,
        (("severity", "warning"),): 2,
        (("severity", "critical"),): 1,
    }

    job = (("job_name", "status"),)
    assert m["scout_job_runs_total"][job] == 2
    assert m["scout_job_errors_total"][job] == 1
    assert m["scout_job_last_success_timestamp_seconds"][job] == (NOW + timedelta(seconds=2)).timestamp()
    # Registered jobs that never ran still show up, with zero counts and no success timestamp.
    assert m["scout_job_runs_total"][(("job_name", "analysis"),)] == 0
    assert (("job_name", "analysis"),) not in m["scout_job_last_success_timestamp_seconds"]
    assert m["scout_job_running"][job] == 0


def test_tariff_absent_or_broken_reads_zero(store, tmp_path):
    assert scrape(make_app(store, tmp_path, tariff=None))["scout_tariff_loaded"] == {(): 0}
    broken = tmp_path / "tariff.yaml"
    broken.write_text("plan: [nope\n")
    m = scrape(make_app(store, tmp_path, tariff=broken))
    assert m["scout_tariff_loaded"] == {(): 0}
    assert "scout_tariff_rate" not in m
    assert m["scout_recommendations"][(("status", "proposed"),)] == 1  # the rest of the scrape is unaffected


def test_metrics_survive_restart(tmp_path):
    db = tmp_path / "scout.db"
    with SqliteStore(db) as s:
        s.record_job_run("analysis", started_at=NOW, finished_at=NOW)
    with SqliteStore(db) as s:
        m = scrape(make_app(s, tmp_path))
    assert m["scout_job_runs_total"][(("job_name", "analysis"),)] == 1


def test_metrics_registry_is_not_the_global_one(store, tmp_path):
    from prometheus_client import REGISTRY

    with TestClient(make_app(store, tmp_path)) as client:
        assert client.get("/metrics").status_code == 200
        assert client.app.state.runtime.registry is not REGISTRY
    assert REGISTRY.get_sample_value("scout_tariff_loaded") is None


def test_app_lifespan_runs_the_scheduler(tmp_path):
    config = parse_config({"store": {"path": "scout.db"}}, tmp_path)
    with TestClient(create_app(config, sources=Sources())) as client:
        assert client.get("/health").json()["status"] == "ok"
        runtime = client.app.state.runtime
        assert set(runtime.scheduler.jobs) == {"status", "analysis", "redeliver"}
    with SqliteStore(tmp_path / "scout.db") as s:
        runs = {r.name: r.run_count for r in s.list_job_runs()}
    assert runs.get("status", 0) >= 1  # the status cycle ran at start-up, through the real loop


TARGET_LABELS = {"job", "instance"}  # Prometheus sets these from the scrape target; a clash becomes exported_*


def test_no_family_uses_a_prometheus_target_label(store, tmp_path):
    store.record_delivery("report:1", "text", ok=True, at=NOW)
    with TestClient(make_app(store, tmp_path)) as client:
        registry = client.app.state.runtime.registry
        families = list(registry.collect())
        text = client.get("/metrics").text
    assert {f.name for f in families} >= {"scout_job_runs", "scout_deliveries", "scout_tariff_rate", "scout_usage_used"}
    # The collector yields *MetricFamily objects, whose _labelnames hold the declared labels; if that ever
    # stops being true the declared check would go quiet, so insist it sees some.
    assert any(getattr(family, "_labelnames", ()) for family in families)
    for family in families:
        declared = set(getattr(family, "_labelnames", ()))  # the label names, even for a family with no samples
        sampled = {name for sample in family.samples for name in sample.labels}
        assert not (declared | sampled) & TARGET_LABELS, family.name
    for family in text_string_to_metric_families(text):
        for sample in family.samples:
            assert not set(sample.labels) & TARGET_LABELS, sample.name


def test_target_label_check_bites():
    from prometheus_client.core import GaugeMetricFamily

    family = GaugeMetricFamily("scout_x", "x", labels=["job"])
    family.add_metric(["status"], 1)
    assert set(family._labelnames) & TARGET_LABELS
    assert {n for s in family.samples for n in s.labels} & TARGET_LABELS


def samples(collector) -> dict[str, dict[tuple, float]]:
    out: dict[str, dict[tuple, float]] = {}
    for family in collector.collect():
        for sample in family.samples:
            out.setdefault(sample.name, {})[tuple(sorted(sample.labels.items()))] = sample.value
    return out


def test_reports_are_counted_by_kind_path_and_fallback_reason(store, tmp_path):
    store.add_report("morning", NOW, "model text", {"path": "llm", "fallback_reason": None})
    store.add_report("status", NOW, "t", {"path": "template", "fallback_reason": "error"})
    store.add_report("status", NOW, "t", {"path": "template", "fallback_reason": "error"})
    store.add_report("status", NOW, "t", {"path": "template", "fallback_reason": "Bad\nValue {x}"})
    store.add_report("evening", NOW, "t", {})  # an old row without meta
    m = scrape(make_app(store, tmp_path))
    labels = lambda k, p, r: (("fallback_reason", r), ("kind", k), ("path", p))  # noqa: E731
    assert m["scout_reports_total"] == {
        labels("morning", "llm", ""): 1,
        labels("status", "template", "error"): 2,
        labels("status", "template", "other"): 1,  # stored JSON never becomes a free-text label
        labels("evening", "", ""): 1,
    }


def test_signal_rules_report_unknown_as_down_never_as_clear():
    from types import SimpleNamespace

    from argus_scout.server.metrics import ScoutCollector
    from argus_scout.signals import Evaluation

    hit = SimpleNamespace(rule="high-load")
    evaluation = Evaluation(
        [hit, SimpleNamespace(rule="high-load")],
        evaluated={"high-load", "memory-low"},
        unknown={"disk-filling": "prometheus unavailable", "shift-heavy-loads": "skipped: no tariff"},
    )
    m = samples(ScoutCollector(analysis=SimpleNamespace(evaluation=evaluation)))
    assert m["scout_signal_rule_up"] == {
        (("rule", "high-load"),): 1,
        (("rule", "memory-low"),): 1,
        (("rule", "disk-filling"),): 0,
        (("rule", "shift-heavy-loads"),): 0,
    }
    # An unknown rule has no hits sample at all: 0 hits would read as "clear".
    assert m["scout_signal_rule_hits"] == {(("rule", "high-load"),): 2, (("rule", "memory-low"),): 0}
    # Before any cycle (or after a skipped one) both families are empty, not zero.
    assert "scout_signal_rule_up" not in samples(ScoutCollector(analysis=SimpleNamespace(evaluation=None)))


async def test_the_app_exports_the_analysis_cycles_evaluation(store, tmp_path):
    from argus_scout.cycles import AnalysisCycle
    from argus_scout.prometheus import PrometheusUnavailable

    class DownPrometheus:
        async def query(self, *_a, **_k):
            raise PrometheusUnavailable("connection refused")

        async def query_range(self, *_a, **_k):
            raise PrometheusUnavailable("connection refused")

    signals = tmp_path / "signals.yaml"
    signals.write_text(
        "rules:\n  - name: high-load\n    query: node_load1\n    op: '>'\n    threshold: 4\n"
        "    severity: warning\n    recommendation: {title: Load high, action: look}\n"
    )
    config = parse_config(
        {"files": {"signals": str(signals)}, "services": {"prometheus_url": "http://prom:9090"}}, tmp_path
    )
    app = create_app(config, store=store, sources=Sources(), clock=lambda: NOW, run_scheduler=False)
    with TestClient(app) as client:
        runtime = client.app.state.runtime
        cycle = runtime.scheduler.jobs["analysis"].func
        assert isinstance(cycle, AnalysisCycle)
        assert "scout_signal_rule_up{" not in client.get("/metrics").text  # no cycle yet: nothing, not zero
        cycle.client = DownPrometheus()
        await cycle()
        text = client.get("/metrics").text
    assert 'scout_signal_rule_up{rule="high-load"} 0.0' in text
    assert 'scout_signal_rule_hits{rule="high-load"}' not in text
