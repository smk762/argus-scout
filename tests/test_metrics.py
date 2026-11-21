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

    job = (("job", "status"),)
    assert m["scout_job_runs_total"][job] == 2
    assert m["scout_job_errors_total"][job] == 1
    assert m["scout_job_last_success_timestamp_seconds"][job] == (NOW + timedelta(seconds=2)).timestamp()
    # Registered jobs that never ran still show up, with zero counts and no success timestamp.
    assert m["scout_job_runs_total"][(("job", "analysis"),)] == 0
    assert (("job", "analysis"),) not in m["scout_job_last_success_timestamp_seconds"]
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
    assert m["scout_job_runs_total"][(("job", "analysis"),)] == 1


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
