"""Regression tests for the HTTP API security review: DNS rebinding, decisions on content the person didn't see,
source enumeration, the docs under read_auth, batch atomicity, plugin errors, CRLF reasons and the audit clock.

Each test here failed on the code before the fix (origin/dev 8abd711).
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from typing import Any

import pytest
from api_helpers import AUTH, BASE_URL, NOW, OWN_TOKEN, TOKEN, make_app, make_config
from fastapi.testclient import TestClient
from pydantic import BaseModel
from test_engine import Clock, hit, ran

from argus_scout.approvals import StandingApprovals
from argus_scout.engine import RecommendationEngine
from argus_scout.recommendations import Recommendation, Status
from argus_scout.server.app import create_app
from argus_scout.server.guards import HOST_OPEN_PATHS, iter_routes
from argus_scout.severity import Severity
from argus_scout.sources import BoundSource, SourceError, Sources, build_sources
from argus_scout.store import SqliteStore, StaleRecommendation

REBOUND = "http://evil.example:8400"
PARAMS = {"source": "hook", "event_id": "e1", "rec_id": "r1", "report_id": "1"}


@pytest.fixture(autouse=True)
def tokens(monkeypatch):
    monkeypatch.setenv("SCOUT_API_TOKEN", TOKEN)
    monkeypatch.setenv("OWN_TOKEN", OWN_TOKEN)


@pytest.fixture
def store():
    with SqliteStore(":memory:") as s:
        yield s


def client_for(app, base_url: str = BASE_URL, **kw: Any) -> TestClient:
    return TestClient(app, base_url=base_url, **kw)


def concrete(path: str) -> str:
    return re.sub(r"\{(\w+)(:\w+)?\}", lambda m: PARAMS[m.group(1)], path)


def every_request(app) -> list[tuple[str, str]]:
    """``(method, concrete path)`` for every method of every route the app serves."""
    return [
        (method, concrete(view.path))
        for view in iter_routes(app)
        for method in sorted(view.methods - {"HEAD"} or {"GET"})
    ]


# --- 1. DNS rebinding ----------------------------------------------------------------------------------------


@pytest.mark.parametrize("read_auth", [False, True])
def test_a_rebinding_host_is_refused_on_every_route(store, tmp_path, read_auth):
    """A page on evil.example rebound to 127.0.0.1 sends ``Host: evil.example``: no route answers it, read or write,
    with or without read_auth (it would carry no token, but it must not even learn the route exists)."""
    app = make_app(store, tmp_path, read_auth=read_auth)
    with client_for(app, REBOUND) as client:
        requests = every_request(app)
        expected = {"/wall", "/api/events", "/api/recommendations"} | (
            set() if read_auth else {"/docs", "/openapi.json"}
        )
        assert expected <= {p for _, p in requests}
        for method, path in requests:
            if path in HOST_OPEN_PATHS:
                continue
            response = client.request(method, path, headers=AUTH, json={"by": "me", "revision": 0})
            assert response.status_code == 400, f"{method} {path} answered a rebinding Host"
            assert "Host" in response.json()["detail"]
            assert "evil.example" not in response.text  # nothing echoed back into the attacker's page


def test_health_and_metrics_answer_any_host(store, tmp_path):
    """Liveness and the scrape target hold no event or report content and are reached by service names
    (Prometheus scrapes ``scout:8400`` in the quickstart), so they stay open to every Host."""
    with client_for(make_app(store, tmp_path), "http://scout:8400") as client:
        assert client.get("/health").status_code == 200
        assert client.get("/metrics").status_code == 200
        assert client.get("/wall").status_code == 400


@pytest.mark.parametrize(
    "base_url",
    ["http://127.0.0.1:8400", "http://localhost:8400", "http://[::1]:8400", "http://192.0.2.7", "http://LOCALHOST"],
)
def test_ip_literals_and_localhost_pass(store, tmp_path, base_url):
    with client_for(make_app(store, tmp_path), base_url) as client:
        for path in ("/wall", "/api/events", "/api/recommendations"):
            assert client.get(path).status_code == 200, (base_url, path)


def test_a_configured_trusted_host_passes_reads_and_same_origin_writes(store, tmp_path):
    app = make_app(store, tmp_path, trusted_hosts=["scout.lan"])
    with client_for(app, "http://Scout.LAN:8400") as client:
        assert client.get("/wall").status_code == 200
        # A page served from scout.lan writing to its own server: the same name is trusted for the Origin check too.
        response = client.post(
            "/api/events/hook", headers={**AUTH, "Origin": "http://scout.lan:8400"}, json={"summary": "up"}
        )
        assert response.status_code == 200, response.text
    with client_for(app, "http://other.lan:8400") as client:
        assert client.get("/wall").status_code == 400


def test_trusted_hosts_config_is_validated(tmp_path):
    assert make_config(tmp_path, trusted_hosts=None).server.trusted_hosts == []
    assert make_config(tmp_path, trusted_hosts=[" Scout.Lan "]).server.trusted_hosts == ["scout.lan"]
    for bad in ("scout.lan:8400", "http://scout.lan", "", "a b"):
        with pytest.raises(Exception, match="trusted_hosts"):
            make_config(tmp_path, trusted_hosts=[bad])


# --- 2. decisions are on the content the person saw ------------------------------------------------------------


@pytest.fixture
def clock():
    return Clock(NOW)


@pytest.fixture
def engine(store, clock):
    return RecommendationEngine(store, StandingApprovals(), clock=clock)


@pytest.fixture
def client(store, tmp_path, clock):
    app = create_app(make_config(tmp_path), store=store, clock=clock, run_scheduler=False)
    with client_for(app) as c:
        yield c


def proposed(engine) -> Recommendation:
    [rec] = engine.run_cycle(ran(hit(value=90.0))).new
    return rec


def test_reads_expose_the_revision(engine, client):
    rec = proposed(engine)
    one = client.get(f"/api/recommendations/{rec.id}").json()
    [listed] = client.get("/api/recommendations").json()["recommendations"]
    assert one["revision"] == listed["revision"] == rec.revision >= 1
    assert f"rev {rec.revision}" in client.get("/wall").text


def test_a_decision_on_content_the_person_never_saw_is_refused(engine, store, client, clock):
    """(b) The person viewed the 90% wording; the engine has since rewritten it. Approving says 409, not yes."""
    rec = proposed(engine)
    viewed = client.get(f"/api/recommendations/{rec.id}").json()
    clock.advance(minutes=15)
    engine.run_cycle(ran(hit(value=95.0)))  # same status, new title, action and evidence

    response = client.post(
        f"/api/recommendations/{rec.id}/approve", headers=AUTH, json={"by": "sam", "revision": viewed["revision"]}
    )
    assert response.status_code == 409
    assert "changed since you viewed it" in response.json()["detail"]
    stored = store.get_recommendation(rec.id)
    assert stored.status is Status.PROPOSED and stored.title.endswith("95% full")

    current = client.get(f"/api/recommendations/{rec.id}").json()
    assert current["revision"] > viewed["revision"]
    response = client.post(
        f"/api/recommendations/{rec.id}/approve", headers=AUTH, json={"by": "sam", "revision": current["revision"]}
    )
    assert response.status_code == 200
    assert response.json()["revision"] == current["revision"] + 1


def test_a_decision_never_reverts_the_engines_escalation(engine, store, client, clock, monkeypatch):
    """(a) The lost update: the route's copy predates an engine escalation. The decision lands on the stored row,
    so the escalated severity and wording survive the approval."""
    rec = proposed(engine)
    stale = store.get_recommendation(rec.id)
    clock.advance(minutes=15)
    engine.run_cycle(ran(hit(value=97.0, severity=Severity.CRITICAL)))
    escalated = store.get_recommendation(rec.id)
    assert escalated.severity is Severity.CRITICAL

    # Whatever the route reads outside the store's transaction is the pre-escalation copy.
    monkeypatch.setattr(store, "get_recommendation", lambda rec_id: stale)
    response = client.post(
        f"/api/recommendations/{rec.id}/approve", headers=AUTH, json={"by": "sam", "revision": escalated.revision}
    )
    monkeypatch.undo()
    assert response.status_code == 200, response.text
    stored = store.get_recommendation(rec.id)
    assert stored.status is Status.APPROVED
    assert stored.severity is Severity.CRITICAL and stored.title.endswith("97% full")
    assert stored.evidence == escalated.evidence


def test_the_revision_is_required(engine, client):
    rec = proposed(engine)
    response = client.post(f"/api/recommendations/{rec.id}/approve", headers=AUTH, json={"by": "sam"})
    assert response.status_code == 422


def test_store_checks_and_bumps_the_revision():
    with SqliteStore(":memory:") as s:
        rec = Recommendation(title="t")
        s.save_recommendation(rec)
        assert rec.revision == 1 and s.get_recommendation(rec.id).revision == 1
        rec.title = "u"
        s.save_recommendation(rec, expected_revision=1)
        assert rec.revision == 2
        with pytest.raises(StaleRecommendation):
            s.save_recommendation(rec, expected_revision=1)
        assert s.get_recommendation(rec.id).title == "u"
        with pytest.raises(StaleRecommendation):
            s.save_recommendation(Recommendation(title="ghost"), expected_revision=0)


def test_engine_save_is_refused_when_content_changed_since_its_load(store, clock):
    """The engine's load-then-save carries the revision too: a same-status write in its window wins."""
    engine = RecommendationEngine(store, StandingApprovals(), clock=clock)
    rec = proposed(engine)
    original = store.save_recommendation
    calls = {"n": 0}

    def save(r, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:  # someone else edits the row between the engine's load and its save
            other = store.get_recommendation(r.id)
            other.impact = "edited elsewhere"
            original(other)
        original(r, **kwargs)

    store.save_recommendation = save  # type: ignore[method-assign]
    clock.advance(minutes=15)
    report = engine.run_cycle(ran(hit(value=95.0)))
    store.save_recommendation = original  # type: ignore[method-assign]
    assert report.refreshed == []
    assert store.get_recommendation(rec.id).impact == "edited elsewhere"


# --- 3. no source enumeration without credentials ------------------------------------------------------------


@pytest.mark.parametrize("source", ["hook", "own", "poller", "no-such-source"])
def test_unauthenticated_pushes_get_the_same_401(store, tmp_path, source):
    with client_for(make_app(store, tmp_path)) as client:
        for headers in ({}, {"Authorization": "Bearer wrong"}):
            response = client.post(f"/api/events/{source}", headers=headers, json={"summary": "x"})
            assert response.status_code == 401, (source, headers)
            assert response.json() == {"detail": "bearer token required"}


def test_the_server_token_still_learns_what_is_wrong(store, tmp_path):
    with client_for(make_app(store, tmp_path)) as client:
        assert client.post("/api/events/no-such-source", headers=AUTH, json={}).status_code == 404
        assert client.post("/api/events/poller", headers=AUTH, json={}).status_code == 405
        # A source with its own token takes only that token, as before.
        assert client.post("/api/events/own", headers=AUTH, json={"summary": "x"}).status_code == 401
        own = {"Authorization": f"Bearer {OWN_TOKEN}"}
        assert client.post("/api/events/own", headers=own, json={"summary": "x"}).status_code == 200
        assert client.post("/api/events/hook", headers=own, json={"summary": "x"}).status_code == 401


def test_no_server_token_closes_unknown_and_known_sources_alike(store, tmp_path, monkeypatch):
    monkeypatch.delenv("SCOUT_API_TOKEN")
    with client_for(make_app(store, tmp_path)) as client:
        known = client.post("/api/events/hook", json={"summary": "x"})
        unknown = client.post("/api/events/no-such-source", json={"summary": "x"})
        poller = client.post("/api/events/poller", json={"summary": "x"})
        assert known.status_code == unknown.status_code == poller.status_code == 503
        assert known.json() == unknown.json() == poller.json()


# --- 4. the API docs under read_auth ---------------------------------------------------------------------------


def test_docs_are_off_under_read_auth(store, tmp_path):
    with client_for(make_app(store, tmp_path, read_auth=True)) as client:
        for path in ("/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"):
            assert client.get(path).status_code == 404, path
            assert client.get(path, headers=AUTH).status_code == 404, path
    with client_for(make_app(store, tmp_path)) as client:
        assert client.get("/openapi.json").status_code == 200
        assert client.get("/docs").status_code == 200


# --- 5. a push batch is one transaction ------------------------------------------------------------------------


class FailingStore(SqliteStore):
    """Fails the ``fail_on``-th event upsert, as a full disk or a locked database would."""

    fail_on = 0
    upserts = 0

    def upsert_event(self, event, *, now=None) -> bool:
        self.upserts += 1
        if self.upserts == self.fail_on:
            raise sqlite3.OperationalError("database or disk is full")
        return super().upsert_event(event, now=now)


def test_a_store_error_mid_batch_stores_nothing(tmp_path):
    with FailingStore(":memory:") as store:
        store.fail_on = 2
        batch = [{"event_id": f"e{i}", "summary": f"event {i}"} for i in range(3)]
        with client_for(make_app(store, tmp_path), raise_server_exceptions=False) as client:
            assert client.post("/api/events/hook", headers=AUTH, json=batch).status_code == 500
            assert store.open_events() == []
            retry = client.post("/api/events/hook", headers=AUTH, json=batch)
        assert retry.status_code == 200
        assert [e["outcome"] for e in retry.json()["events"]] == ["new", "new", "new"]


# --- 6. a plugin's own errors are a 4xx, not a 500 -------------------------------------------------------------


class _Cfg(BaseModel):
    name: str
    kind: str = "fussy"


@dataclass
class Fussy:
    """A push plugin whose parse fails the way third-party code does."""

    error: Exception
    kind: str = "fussy"
    config_model: type[BaseModel] = _Cfg

    def parse(self, cfg, body):
        raise self.error

    def authorize(self, cfg, authorization, *, fallback_env=None, environ=None) -> None:
        return None


def _json_error() -> json.JSONDecodeError:
    try:
        json.loads("{/secret/path")
    except json.JSONDecodeError as exc:
        return exc
    raise AssertionError("unreachable")


@pytest.mark.parametrize(
    "error", [SourceError("upstream /secret/path broke"), ValueError("internal detail /secret/path"), _json_error()]
)
def test_plugin_parse_errors_are_400_without_internals(store, tmp_path, error):
    config = make_config(tmp_path)
    sources = build_sources(config)
    sources = Sources([*sources.sources, BoundSource(Fussy(error), _Cfg(name="fussy"))])
    app = create_app(config, store=store, sources=sources, clock=lambda: NOW, run_scheduler=False)
    with client_for(app) as client:
        response = client.post("/api/events/fussy", headers=AUTH, json={"summary": "x"})
    assert response.status_code == 400
    assert "secret" not in response.text


# --- 7. CRLF in a reason ---------------------------------------------------------------------------------------


def test_a_crlf_reason_is_normalised(engine, store, client):
    rec = proposed(engine)
    revision = client.get(f"/api/recommendations/{rec.id}").json()["revision"]
    body = {"by": "sam", "reason": "line one\r\nline two\rline three", "revision": revision}
    response = client.post(f"/api/recommendations/{rec.id}/reject", headers=AUTH, json=body)
    assert response.status_code == 200, response.text
    assert store.get_recommendation(rec.id).rejection_reason == "line one\nline two\nline three"
    assert store.recommendation_history(rec.id)[-1].reason == "line one\nline two\nline three"


# --- 8. the audit trail uses the app's clock -------------------------------------------------------------------


def test_decisions_use_the_injected_clock(engine, store, client, clock):
    rec = proposed(engine)
    clock.advance(hours=1)
    revision = client.get(f"/api/recommendations/{rec.id}").json()["revision"]
    response = client.post(
        f"/api/recommendations/{rec.id}/approve", headers=AUTH, json={"by": "sam", "revision": revision}
    )
    assert response.status_code == 200
    stored = store.get_recommendation(rec.id)
    assert stored.approved_at == stored.updated_at == clock()
    assert store.recommendation_history(rec.id)[-1].at == clock()


def test_revision_migration_reads_old_rows_as_zero_and_replays_harmlessly(tmp_path):
    from argus_scout.store.sqlite import MIGRATIONS, SCHEMA_VERSION

    db = tmp_path / "old.db"
    with SqliteStore(db) as s:
        rec = Recommendation(title="from v0.0")
        s.save_recommendation(rec)
    conn = sqlite3.connect(db)
    conn.execute("DROP TABLE recommendation_revisions")  # back to the schema before migration 9
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION - 1}")
    conn.commit()
    conn.close()
    assert "recommendation_revisions" in MIGRATIONS[-1]
    with SqliteStore(db) as s:
        assert s.schema_version == SCHEMA_VERSION
        old = s.get_recommendation(rec.id)
        assert old.revision == 0
        old.title = "edited"
        s.save_recommendation(old, expected_revision=0)
        assert s.get_recommendation(rec.id).revision == 1
        s._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION - 1}")
    with SqliteStore(db) as s:  # migration 9 again over its own table: nothing lost
        assert s.get_recommendation(rec.id).revision == 1


HOST_TABLE = [
    "127.0.0.1",
    "127.0.0.1:8400",
    "10.0.0.5",
    "0.0.0.0:80",
    "localhost",
    "localhost:8400",
    "LOCALHOST",
    "LocalHost:1",
    "localhost.",
    "localhost.localdomain",
    "[::1]",
    "[::1]:8400",
    "[fe80::1%25eth0]:80",
    "::1",
    "[2001:db8::1]:443",
    "evil.example",
    "evil.example:8400",
    "EVIL.Example.",
    "127.0.0.1.nip.io",
    "xn--bcher-kva.example",
    "bücher.example",
    "scout",
    "0x7f.1",
    "2130706433",
    "",
    ":8400",
    "[::1",
    "a b",
    "host:notaport",
    "user@127.0.0.1",
    "127.0.0.1/x",
]


def test_host_rule_matches_argus_cortex():
    """HostGuard's Host test is local (cortex's helpers are private); drift from cortex's same-host rule shows here."""
    import argus_cortex.server as cortex

    from argus_scout.server.guards import host_name, rebind_proof

    theirs_name = getattr(cortex, "_host_name", None)
    theirs_proof = getattr(cortex, "_rebind_proof", None)
    if theirs_name is None or theirs_proof is None:
        pytest.skip("argus_cortex.server no longer has _host_name/_rebind_proof: compare with its public helper")
    for host in HOST_TABLE:
        ours, theirs = host_name(host), theirs_name(host)
        assert ours == theirs, host
        if ours is not None:
            assert rebind_proof(ours) == theirs_proof(ours), host
    assert rebind_proof("127.0.0.1") and rebind_proof("::1") and rebind_proof("localhost")
    assert not rebind_proof("evil.example") and not rebind_proof("localhost.")
