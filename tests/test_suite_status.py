"""Suite status from Prometheus (#31): health, versions and deploys of the Argus services, as status events and
an on-request report. Prometheus is faked: a stub fetcher answers each of the reading's queries from a table."""

from __future__ import annotations

import ast
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from api_helpers import AUTH, scout_client
from argus_cortex.net import Reply
from typer.testing import CliRunner

from argus_scout.cli import app as cli
from argus_scout.config import SuiteStatusSource, parse_config
from argus_scout.cycles import PollJob
from argus_scout.inbox import Inbox
from argus_scout.narrative import Narrator, SuiteNotConfigured, SuiteUnavailable, suite_reader
from argus_scout.prometheus import PrometheusClient
from argus_scout.server.app import create_app
from argus_scout.severity import Severity
from argus_scout.sources import SourceError, build_sources
from argus_scout.sources.suite_status import poll_once, read_once
from argus_scout.store import SqliteStore
from argus_scout.suite import queries, render_report, spoken_summary, suite_events, version_key

BASE = "http://prometheus.lan:9090"
NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
SRC = Path(__file__).resolve().parent.parent / "src" / "argus_scout"
TOKEN = "server-secret"


def suite_config(**keys) -> SuiteStatusSource:
    return parse_config(
        {"sources": [{"kind": "suite_status", "name": "suite", "prometheus_url": BASE, **keys}]}
    ).sources[0]


CFG = suite_config()


def target(peer: str, node: str, **labels: str) -> dict[str, str]:
    return {"argus_peer": peer, "node": node, "container": f"argus-{peer}", "instance": f"{node}.lan:8100", **labels}


class FakePrometheus:
    """Answers a reading's queries by name (see ``suite.queries``). ``set`` replaces one answer; a query with no
    answer set returns an empty vector, and ``fail`` makes one (or every) query a server error."""

    def __init__(self, cfg: SuiteStatusSource = CFG) -> None:
        self.names = {promql: name for name, promql in queries(cfg).items()}
        self.answers: dict[str, list[tuple[dict, float]]] = {}
        self.fail: set[str] = set()
        self.calls: list[tuple[str, dict]] = []

    def set(self, name: str, *samples: tuple[dict, float]) -> FakePrometheus:
        assert name in self.names.values(), name
        self.answers[name] = list(samples)
        return self

    async def __call__(self, url: str, **kwargs) -> Reply:
        self.calls.append((url, kwargs))
        name = self.names[parse_qs(urlsplit(url).query)["query"][0]]
        if name in self.fail or "*" in self.fail:
            return Reply(503, {}, b"down", BASE)
        result = [{"metric": m, "value": [NOW.timestamp(), str(v)]} for m, v in self.answers.get(name, [])]
        doc = {"status": "success", "data": {"resultType": "vector", "result": result}}
        return Reply(200, {}, json.dumps(doc).encode(), BASE)

    @property
    def client(self) -> PrometheusClient:
        return PrometheusClient(BASE, fetcher=self)

    # --- scenarios: what Prometheus holds for one service ---

    def service(
        self,
        peer: str,
        node: str,
        *,
        up: float | None = 1,
        code: float | None = 200,
        ok: float | None = 1,
        version: str | None = "1.0.0",
        recent: tuple[str, ...] = (),
        answered: bool = True,
        ok_in_window: float | None = 1,
        seen: bool = True,
    ) -> FakePrometheus:
        """Add one service. ``up``/``code``/``ok`` ``None`` leaves that series out now; ``answered`` is whether
        anything answered within ``down_for``; ``ok_in_window`` is the guarded max of ``probe_success``."""
        labels, probe = target(peer, node, job="argus"), target(peer, node, job="argus-health")

        def add(name: str, metric: dict, value: float | None) -> None:
            if value is not None:
                self.answers.setdefault(name, []).append((metric, value))

        add("up_now", labels, up)
        add("code_now", probe, code)
        add("ok_now", probe, ok if code is not None else None)
        present = up is not None or code is not None
        add("up_window", labels, (1 if answered else 0) if up is not None else None)
        add("code_window", probe, ((code or 200) if answered else 0) if code is not None else None)
        add("ok_window", probe, ok_in_window if code is not None else None)
        add("up_seen", labels, 1 if seen else None)
        add("code_seen", probe, 200 if seen and code is not None else None)
        build = {**labels, "service": f"argus-{peer}", "service_version": "1.1"}
        if version and present:
            add("build_now", {**build, "version": version}, 1)
        for old in (*recent, *((version,) if version else ())):
            add("build_recent", {**build, "version": old}, 1)
        return self


async def reading(prom: FakePrometheus, cfg: SuiteStatusSource = CFG):
    return await read_once(cfg, client=prom.client, now=NOW)


def by_condition(events) -> dict[str, list]:
    found: dict[str, list] = {}
    for event in events:
        found.setdefault(event.payload["condition"], []).append(event)
    return found


# --- what is asked ---------------------------------------------------------------------------------------------


def test_queries_select_only_suite_series_and_never_the_probe_jobs_own_up():
    q = queries(suite_config(health_job="bb", down_for=90, degraded_for=300, deploy_window=1800, forget_after=7200))
    assert all('argus_peer!=""' in promql for promql in q.values()), "only series a suite target carries"
    assert q["up_now"] == 'up{argus_peer!="",job!="bb"}'
    assert q["up_window"] == f"max_over_time({q['up_now']}[90s])" and "[7200s]" in q["up_seen"]
    assert "[300s]" in q["ok_window"] and "offset 300s" in q["ok_window"]
    assert q["build_recent"] == 'last_over_time(argus_build_info{argus_peer!=""}[1800s])'


@pytest.mark.parametrize("job", ['x"} or vector(1) #', "a b", "", "job{", "x\ny"])
def test_the_one_configurable_name_in_a_query_is_a_plain_token(job):
    from argus_scout.config import ConfigError

    with pytest.raises(ConfigError, match="health_job"):
        suite_config(health_job=job)


async def test_a_reading_asks_only_the_configured_prometheus_with_read_only_instant_queries():
    prom = FakePrometheus().service("lens", "alpha")
    await reading(prom)
    assert len(prom.calls) == len(queries(CFG)) == 10
    for url, kwargs in prom.calls:
        parts = urlsplit(url)
        assert f"{parts.scheme}://{parts.netloc}" == BASE and parts.path == "/api/v1/query"
        assert kwargs["method"] == "GET" and kwargs["max_redirects"] == 0
        assert kwargs["allow_hosts"] == ["prometheus.lan:9090"]


def test_the_suite_modules_have_no_network_client_of_their_own():
    """Scout never probes a service: the suite code may reach the network only through PrometheusClient."""
    for rel in ("suite.py", "sources/suite_status.py"):
        tree = ast.parse((SRC / rel).read_text())
        modules = {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)} | {
            a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names
        }
        assert not {m for m in modules if m.split(".")[0] in {"httpx", "socket", "urllib3", "requests", "aiohttp"}}
        assert "argus_cortex.net" not in modules and "urllib.request" not in modules, rel


# --- states ----------------------------------------------------------------------------------------------------


async def test_states_from_up_the_health_probe_and_build_info():
    prom = (
        FakePrometheus()
        .service("lens", "alpha", version="0.14.2")
        .service("curator", "alpha", code=200, ok=0, ok_in_window=0, version="0.2.7")  # 200 without "ok"
        .service("mask", "alpha", code=503, ok=0, ok_in_window=1)  # starting, but it was ok a moment ago
        .service("forge", "alpha", up=0, code=0, ok=0, answered=False, version=None)
        .service("atlas", "alpha", code=None, version="0.5.0")  # metrics scraped, no health probe
        .service("proof", "beta", code=500, ok=0, ok_in_window=None)  # only just probed: not "for 10m" yet
    )
    status = await reading(prom)
    states = {s.peer: (s.state, s.held, s.detail) for s in status.services}
    assert states == {
        "lens": ("ok", False, ""),
        "curator": ("degraded", True, "/health does not answer ok"),
        "mask": ("starting", False, "/health answers 503"),
        "forge": ("down", True, "no answer"),
        "atlas": ("up", False, "health not probed"),
        "proof": ("degraded", False, "/health answers HTTP 500"),
    }
    lens = next(s for s in status.services if s.peer == "lens")
    assert (lens.where, lens.container, lens.package, lens.version) == ("alpha", "argus-lens", "argus-lens", "0.14.2")


async def test_down_is_not_raised_until_nothing_answered_for_the_whole_window():
    blip = FakePrometheus().service("lens", "alpha", up=0, code=0, ok=0, answered=True)
    status = await reading(blip)
    assert [(s.state, s.held) for s in status.services] == [("down", False)]
    assert suite_events(status, CFG) == []


async def test_a_service_that_is_no_longer_a_target_is_down_then_forgotten():
    # argus-iris lists only running containers, so a stopped one disappears from Prometheus instead of up == 0.
    gone = FakePrometheus().service("lens", "alpha", up=None, code=None, answered=False, version="0.14.2")
    status = await reading(gone)
    (service,) = status.services
    assert (service.state, service.held, service.detail) == ("down", True, "no longer a scrape target")
    (event,) = suite_events(status, CFG)
    assert event.summary == "lens on alpha is down" and "Last version seen: 0.14.2" in event.body_md
    # Past forget_after Prometheus's range no longer holds it: nothing is listed, and nothing is raised.
    forgotten = await reading(FakePrometheus())
    assert forgotten.services == () and suite_events(forgotten, CFG) == []


async def test_metrics_scraped_but_the_probe_unanswered_is_not_down():
    status = await reading(FakePrometheus().service("lens", "alpha", up=1, code=0, ok=0))
    (service,) = status.services
    assert (service.state, service.detail) == ("up", "the health probe got no answer")
    assert suite_events(status, CFG) == []


async def test_a_probe_job_under_another_name_is_still_not_read_as_the_service_answering():
    # health_job is left at its default, but the probe job is called "bb": its own `up` is 1 (the blackbox
    # exporter answered) while the service gives no answer. That `up` must not make the service look alive.
    labels = target("lens", "alpha", job="bb")
    prom = FakePrometheus()
    for name in ("up_now", "up_window", "up_seen"):
        prom.set(name, (labels, 1))
    prom.set("code_now", (labels, 0)).set("ok_now", (labels, 0)).set("code_window", (labels, 0))
    prom.set("code_seen", (labels, 200))
    status = await reading(prom)
    assert [(s.state, s.held, s.detail) for s in status.services] == [("down", True, "no answer")]


# --- events ----------------------------------------------------------------------------------------------------


async def test_events_for_down_degraded_version_change_and_skew():
    prom = (
        FakePrometheus()
        .service("forge", "alpha", up=0, code=0, ok=0, answered=False, version=None)
        .service("curator", "alpha", code=200, ok=0, ok_in_window=0, version="0.2.7")
        .service("mask", "alpha", code=503, ok=0, ok_in_window=0, version="0.4.0")
        .service("lens", "alpha", version="0.15.0", recent=("0.14.2",))
        .service("lens", "beta", version="0.14.2")
        .service("atlas", "alpha", version="0.5.0")
    )
    events = by_condition(suite_events(await reading(prom), CFG))
    assert {k: [e.summary for e in v] for k, v in events.items()} == {
        "down": ["forge on alpha is down"],
        "health": ["curator on alpha is degraded", "mask on alpha is still starting"],
        "version": ["lens on alpha changed version from 0.14.2 to 0.15.0"],
        "skew": ["lens runs different versions: 0.14.2 on beta, 0.15.0 on alpha"],
    }
    assert {k: v[0].severity for k, v in events.items()} == {
        "down": Severity.WARNING,
        "health": Severity.WARNING,
        "version": Severity.INFO,
        "skew": Severity.NOTICE,
    }
    every = [e for found in events.values() for e in found]
    assert all(e.source == "suite" and e.event_id.startswith("suite:") for e in every)
    assert len({e.event_id for e in every}) == len(every)
    assert events["version"][0].payload == {
        "condition": "version",
        "peer": "lens",
        "host": "alpha",
        "service": "argus-lens",
        "version": "0.15.0",
        "previous": "0.14.2",
    }


async def test_severities_are_configurable():
    cfg = suite_config(severity={"down": "critical", "version_change": "notice"})
    prom = FakePrometheus(cfg).service("forge", "alpha", up=0, code=0, ok=0, answered=False)
    prom.service("lens", "alpha", version="2.0", recent=("1.0",))
    events = by_condition(suite_events(await reading(prom, cfg), cfg))
    assert events["down"][0].severity is Severity.CRITICAL and events["version"][0].severity is Severity.NOTICE


async def test_ids_and_summaries_are_stable_while_a_condition_lasts():
    def down():
        return FakePrometheus().service("forge", "alpha", up=0, code=0, ok=0, answered=False, version=None)

    first = suite_events(await reading(down()), CFG)
    later = suite_events(await read_once(CFG, client=down().client, now=NOW + timedelta(hours=3)), CFG)
    assert [(e.event_id, e.fingerprint) for e in first] == [(e.event_id, e.fingerprint) for e in later]
    # A second deploy is a new event; the same one is the same event.
    v2 = FakePrometheus().service("lens", "alpha", version="0.15.0", recent=("0.14.2",))
    v3 = FakePrometheus().service("lens", "alpha", version="0.16.0", recent=("0.15.0",))
    ids = [suite_events(await reading(p), CFG)[0].event_id for p in (v2, v2, v3)]
    assert ids[0] == ids[1] != ids[2]


async def test_a_version_change_is_not_raised_for_a_service_that_reports_no_version_now():
    prom = FakePrometheus().service("lens", "alpha", up=0, code=0, ok=0, answered=False, version=None)
    prom.set("build_recent", ({**target("lens", "alpha"), "version": "0.14.2"}, 1))
    assert [e.payload["condition"] for e in suite_events(await reading(prom), CFG)] == ["down"]


async def test_skew_needs_two_hosts_and_says_which_is_behind_only_when_versions_order():
    same = FakePrometheus().service("lens", "alpha", version="0.15.0").service("lens", "beta", version="0.15.0")
    assert (await reading(same)).skews == ()
    ordered = FakePrometheus().service("lens", "alpha", version="0.15.0").service("lens", "beta", version="0.15.0.dev3")
    status = await reading(ordered)
    assert {s.where: s.behind for s in status.services} == {"alpha": "", "beta": "0.15.0"}
    odd = FakePrometheus().service("lens", "alpha", version="main").service("lens", "beta", version="0.15.0")
    status = await reading(odd)
    assert [k.peer for k in status.skews] == ["lens"] and status.behind == [], "differs, but scout won't guess"
    # A down host's version is not part of the comparison: it reports none.
    down = FakePrometheus().service("lens", "alpha", version="0.15.0")
    down.service("lens", "beta", up=0, code=0, ok=0, answered=False, version=None)
    assert (await reading(down)).skews == ()


@pytest.mark.parametrize(
    ("older", "newer"),
    [
        ("0.14.2", "0.15.0"),
        ("0.9", "0.10"),
        ("0.2.3.dev38", "0.2.3"),
        ("0.1.5.dev1+g9b71751", "0.1.5"),
        ("1.0rc1", "1"),
    ],
)
def test_version_order(older, newer):
    assert version_key(older) < version_key(newer)
    assert version_key("1.0") == version_key("1.0.0") == version_key("v1.0+local")
    assert version_key("main") is None and version_key("1.0-custom") is None


async def test_label_values_from_prometheus_are_only_ever_short_plain_text():
    nasty = {"argus_peer": "lens\n## Down\n- everything", "node": "a" * 500, "container": "c\x00\x1b[31m"}
    prom = FakePrometheus().set("up_now", (nasty, 0)).set("up_seen", (nasty, 1)).set("up_window", (nasty, 0))
    status = await reading(prom)
    (service,) = status.services
    assert "\n" not in service.peer and len(service.where) == 80 and "\x1b" not in service.container
    (event,) = suite_events(status, CFG)
    assert "\n" not in event.summary
    text = render_report(status, CFG)
    assert text.count("\n- ") == 2, "one line under Down and one under Services, whatever the label said"


# --- unknown is never all clear --------------------------------------------------------------------------------


@pytest.mark.parametrize("failing", ["*", "up_now", "ok_window", "build_recent"])
async def test_a_reading_prometheus_cannot_complete_raises_and_reports_nothing(failing):
    prom = FakePrometheus().service("lens", "alpha")
    prom.fail.add(failing)
    with pytest.raises(SourceError, match="suite status is unknown"):
        await poll_once(CFG, client=prom.client, now=NOW)


async def test_poll_job_ingests_resolves_cleared_conditions_and_keeps_them_through_an_outage():
    store = SqliteStore(":memory:")
    inbox = Inbox(store, clock=lambda: NOW)
    config = parse_config(
        {"services": {"prometheus_url": BASE}, "sources": [{"kind": "suite_status", "name": "suite"}]}
    )
    (bound,) = build_sources(config).sources
    assert bound.config.prometheus_url == BASE and bound.polls and not bound.pushes
    job = PollJob(bound, inbox)
    prom = FakePrometheus().service("forge", "alpha", up=0, code=0, ok=0, answered=False)
    prom.service("lens", "alpha", version="0.15.0", recent=("0.14.2",))

    class Plugin:
        kind, config_model = bound.plugin.kind, bound.plugin.config_model

        async def poll(self, cfg):
            return await poll_once(cfg, client=prom.client, now=NOW)

    job.source = type(bound)(Plugin(), bound.config)
    await job()
    await job()  # the same reading again: refreshed, not duplicated
    assert sorted(e.summary for e in inbox.open()) == [
        "forge on alpha is down",
        "lens on alpha changed version from 0.14.2 to 0.15.0",
    ]
    prom.fail.add("*")  # Prometheus is out: the poll fails, and nothing is resolved on no information
    with pytest.raises(SourceError):
        await job()
    assert len(inbox.open()) == 2
    prom.fail.clear()
    prom.answers.clear()
    prom.service("forge", "alpha").service("lens", "alpha", version="0.15.0")  # recovered; deploy window passed
    await job()
    assert inbox.open() == [] and job.outcomes["resolved"] == 2
    store.close()


def test_the_source_needs_a_prometheus_and_fails_only_itself():
    config = parse_config({"sources": [{"kind": "suite_status", "name": "suite"}, {"kind": "webhook", "name": "hook"}]})
    built = build_sources(config)
    assert [s.name for s in built.sources] == ["hook"]
    assert built.errors == {"suite": "suite_status: no Prometheus to ask (set services.prometheus_url)"}


def test_the_source_has_no_push_route_and_is_advisory_only():
    plugin = build_sources(parse_config({"sources": [dict(CFG.model_dump())]})).sources[0].plugin
    assert not hasattr(plugin, "parse") and not hasattr(plugin, "authorize")
    public = {name for name in dir(plugin) if not name.startswith("_")}
    assert public == {"kind", "config_model", "poll"}


# --- the report ------------------------------------------------------------------------------------------------


async def full_reading():
    prom = (
        FakePrometheus()
        .service("forge", "alpha", up=0, code=0, ok=0, answered=False, version=None)
        .service("curator", "alpha", code=200, ok=0, ok_in_window=0, version="0.2.7")
        .service("lens", "alpha", version="0.15.0", recent=("0.14.2",))
        .service("lens", "beta", version="0.14.2")
        .service("atlas", "alpha", version="0.5.0")
    )
    return await reading(prom)


async def test_report_lists_what_is_wrong_first_then_every_service_with_its_version():
    text = render_report(await full_reading(), CFG)
    assert text == (
        "Suite status at 2026-06-01 12:00 UTC: 5 services.\n"
        "\n"
        "Down (1)\n"
        "- forge on alpha: down: no answer, no version\n"
        "\n"
        "Degraded or starting (1)\n"
        "- curator on alpha: degraded: /health does not answer ok, 0.2.7\n"
        "\n"
        "Out of date (1)\n"
        "- lens on beta: 0.14.2, behind 0.15.0 running elsewhere\n"
        "\n"
        "Changed version in the last 1h (1)\n"
        "- lens on alpha: 0.14.2 to 0.15.0\n"
        "\n"
        "Services\n"
        "- forge on alpha: down: no answer, no version\n"
        "- curator on alpha: degraded: /health does not answer ok, 0.2.7\n"
        "- atlas on alpha: ok, 0.5.0\n"
        "- lens on alpha: ok, 0.15.0\n"
        "- lens on beta: ok, 0.14.2\n"
    )


async def test_report_never_calls_an_empty_or_healthy_suite_more_than_it_is():
    empty = render_report(await reading(FakePrometheus()), CFG)
    assert "0 services" in empty and "not an all-clear" in empty and "Nothing is down" not in empty
    healthy = await reading(FakePrometheus().service("lens", "alpha", version="0.15.0"))
    assert "Nothing is down, degraded or out of date." in render_report(healthy, CFG)
    assert spoken_summary(healthy) == "Suite status: 1 service. Nothing is down, degraded or out of date."
    assert spoken_summary(await full_reading()) == (
        "Suite status: 5 services. 1 down: forge on alpha. 1 degraded or starting: curator on alpha. "
        "1 out of date: lens on beta."
    )


async def test_two_containers_of_one_peer_on_one_host_are_told_apart():
    prom = FakePrometheus()
    for container in ("lens-a", "lens-b"):
        labels = {"argus_peer": "lens", "node": "alpha", "container": container}
        prom.answers.setdefault("up_now", []).append((labels, 1))
        prom.answers.setdefault("up_seen", []).append((labels, 1))
    text = render_report(await reading(prom), CFG)
    assert "- lens on alpha (lens-a): up" in text and "- lens on alpha (lens-b): up" in text


# --- the narrator, the route and the CLI -----------------------------------------------------------------------


def narrator_for(prom: FakePrometheus | None, store, **source) -> Narrator:
    config = parse_config({"sources": [{"kind": "suite_status", "name": "suite", **source}] if prom else []})
    reader = suite_reader(config, clock=lambda: NOW, prometheus=prom.client if prom else None)
    return Narrator(store, clock=lambda: NOW, suite=reader)


async def test_suite_report_is_a_listing_stored_with_what_the_channels_read():
    with SqliteStore(":memory:") as store:
        prom = FakePrometheus().service("forge", "alpha", up=0, code=0, ok=0, answered=False)
        prom.service("lens", "alpha", version="0.15.0")
        report = await narrator_for(prom, store).report("suite", trigger="request")
        assert report.id and report.kind == "suite" and store.latest_report("suite").text == report.text
        assert report.text.startswith("Suite status at 2026-06-01 12:00 UTC: 2 services.")
        meta = report.meta
        assert (meta["path"], meta["fallback_reason"], meta["model"]) == ("template", "listing", None)
        assert (meta["severity"], meta["title"], meta["trigger"]) == ("warning", "Suite status", "request")
        assert meta["speakable"] == "Suite status: 2 services. 1 down: forge on alpha."
        assert meta["suite"]["states"] == {"ok": 1, "up": 0, "degraded": 0, "starting": 0, "down": 1}


async def test_suite_report_is_refused_not_invented_without_a_source_or_an_answer():
    with SqliteStore(":memory:") as store:
        with pytest.raises(SuiteNotConfigured, match="no suite_status source"):
            await narrator_for(None, store).report("suite")
        prom = FakePrometheus().service("lens", "alpha")
        prom.fail.add("*")
        with pytest.raises(SuiteUnavailable, match="suite status is unknown"):
            await narrator_for(prom, store).report("suite")
        assert store.latest_report("suite") is None, "an outage writes no report"


class Model:
    """A model that must never be asked for a suite report."""

    model = "never"

    async def generate(self, *args, **kwargs):
        raise AssertionError("a suite report is a listing: the model is not asked")


async def test_suite_report_never_asks_the_model():
    with SqliteStore(":memory:") as store:
        narrator = narrator_for(FakePrometheus().service("lens", "alpha"), store)
        narrator.generator = Model()
        report = await narrator.report("suite", use_llm=True)
        assert report.meta["path"] == "template"


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("SCOUT_API_TOKEN", TOKEN)
    store = SqliteStore(tmp_path / "scout.db")

    def make(*, source: bool = True, **server):
        config = parse_config(
            {
                "services": {"prometheus_url": BASE},
                "sources": [{"kind": "suite_status", "name": "suite"}] if source else [],
                "channels": [{"kind": "text", "name": "log", "file": "reports.log"}],
                "server": {"read_only": False, **server},
            },
            tmp_path,
        )
        return create_app(config, store=store, clock=lambda: NOW, run_scheduler=False)

    yield make, store
    store.close()


def use(client, prom: FakePrometheus) -> None:
    """Point the running app's suite reader at the fake Prometheus."""
    runtime = client.app.state.runtime
    runtime.briefings.narrator.suite = suite_reader(runtime.config, clock=lambda: NOW, prometheus=prom.client)


def test_post_reports_kind_suite_writes_and_delivers_the_summary(api, tmp_path):
    make, store = api
    with scout_client(make()) as client:
        use(client, FakePrometheus().service("lens", "alpha", version="0.15.0"))
        reply = client.post("/api/reports", json={"kind": "suite"}, headers=AUTH)
        assert reply.status_code == 200, reply.text
        body = reply.json()
        assert body["kind"] == "suite" and "- lens on alpha: ok, 0.15.0" in body["text"]
        assert body["delivered"] and body["delivery"]["sent"] == ["log"]
        assert "lens on alpha: ok, 0.15.0" in (tmp_path / "reports.log").read_text()
        assert client.get("/api/reports/latest?kind=suite").json()["id"] == body["id"]


def test_post_reports_kind_suite_keeps_every_guard(api):
    make, _ = api
    with scout_client(make()) as client:
        use(client, FakePrometheus().service("lens", "alpha"))
        assert client.post("/api/reports", json={"kind": "suite"}).status_code == 401
    with scout_client(make(read_only=True)) as client:
        assert client.post("/api/reports", json={"kind": "suite"}, headers=AUTH).status_code == 403


def test_post_reports_kind_suite_without_a_source_or_an_answer_writes_nothing(api):
    make, store = api
    with scout_client(make(source=False)) as client:
        reply = client.post("/api/reports", json={"kind": "suite"}, headers=AUTH)
        assert reply.status_code == 409 and "no suite_status source" in reply.json()["detail"]
    with scout_client(make()) as client:
        prom = FakePrometheus().service("lens", "alpha")
        prom.fail.add("*")
        use(client, prom)
        reply = client.post("/api/reports", json={"kind": "suite"}, headers=AUTH)
        assert reply.status_code == 503 and "nothing was written" in reply.json()["detail"]
        assert "prometheus.lan" not in reply.text, "the answer never names the Prometheus URL"
    assert store.latest_report("suite") is None


def test_suite_is_not_a_schedulable_kind(tmp_path):
    from argus_scout.config import ConfigError

    with pytest.raises(ConfigError, match="schedule.briefings.0.kind"):
        parse_config({"schedule": {"briefings": [{"kind": "suite", "at": "07:30"}]}}, tmp_path)


def test_cli_brief_suite_without_a_source_is_an_error_not_a_report(tmp_path):
    (tmp_path / "scout.yaml").write_text("store: {path: scout.db}\n")
    result = CliRunner().invoke(cli, ["brief", "--config", str(tmp_path / "scout.yaml"), "--kind", "suite"])
    assert result.exit_code == 1 and "no suite_status source" in result.output
