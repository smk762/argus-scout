"""Standing approvals over HTTP: approve with ``standing``, list the rules, delete a runtime one.

The guards on the new write route (auth, read-only, origin, Host, body cap) are covered by the route walk in
test_api_guards.py, which counts it among the write routes.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import timedelta

import pytest
from api_helpers import AUTH, OWN_TOKEN, TOKEN, scout_client
from test_engine import T0, Clock, hit, ran

from argus_scout.approvals import StandingApprovals
from argus_scout.config import parse_config
from argus_scout.recommendations import Recommendation, Status
from argus_scout.server.app import create_app
from argus_scout.severity import Severity
from argus_scout.store import SqliteStore

FILE_RULES = """\
rules:
  - id: file-backups
    analyzer: backup
    note: from the file
"""
KEY2 = 'disk-filling{instance="node-a:9100",mountpoint="/srv"}'
KEY3 = 'disk-filling{instance="node-a:9100",mountpoint="/var"}'


@pytest.fixture(autouse=True)
def tokens(monkeypatch):
    monkeypatch.setenv("SCOUT_API_TOKEN", TOKEN)
    monkeypatch.setenv("OWN_TOKEN", OWN_TOKEN)


@pytest.fixture
def store():
    with SqliteStore(":memory:") as s:
        yield s


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def app(store, tmp_path, clock):
    (tmp_path / "approvals.yaml").write_text(FILE_RULES)
    config = parse_config({"server": {"read_only": False}, "files": {"approvals": "approvals.yaml"}}, tmp_path)
    return create_app(config, store=store, clock=clock, run_scheduler=False)


@pytest.fixture
def client(app):
    with scout_client(app) as c:
        yield c


def proposal(store: SqliteStore, title: str = "Move the backup to off-peak", **kw) -> Recommendation:
    fields = {"action": "reschedule", "impact": "low", "analyzer": "power", **kw}
    rec = Recommendation(title=title, **fields)
    store.save_recommendation(rec)
    rec.transition(Status.PROPOSED)
    store.save_recommendation(rec, by="analyzer")
    return rec


def approve(client, rec: Recommendation, standing: dict | None = None, **body):
    payload = {"by": "sam", "revision": rec.revision, **body}
    if standing is not None:
        payload["standing"] = standing
    return client.post(f"/api/recommendations/{rec.id}/approve", headers=AUTH, json=payload)


def raised(report) -> Recommendation:
    """The one recommendation a cycle raised, whether it stayed proposed or a rule approved it."""
    (rec,) = {r.id: r for r in [*report.new, *report.auto_approved]}.values()
    return rec


def runtime_of(client):
    return client.app.state.runtime


# --- approve with standing -----------------------------------------------------------------------------------


def test_approve_with_standing_creates_a_runtime_rule(client, store, clock):
    rec = proposal(store)
    until = T0 + timedelta(days=7)
    response = approve(client, rec, {"scope": "analyzer", "until": until.isoformat()}, reason="fine")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "approved"
    assert body["approval_source"] == "manual" and body["approved_by_rule"] is None  # the person approved this one
    assert body["history"][-1]["by"] == "sam" and body["history"][-1]["source"] == "manual"
    rule = body["standing_rule"]
    assert rule["origin"] == "api" and rule["scope"] == "analyzer" and rule["analyzer"] == "power"
    assert rule["title_exact"] == rec.title and rule["max_impact"] == "low"
    assert rule["created_by"] == "sam" and rule["note"] == f"from recommendation {rec.id}"
    assert rule["expires_at"] == until.isoformat()

    stored = store.load_runtime_rules()
    assert [(r.rule_id, r.created_by) for r in stored] == [(rule["rule_id"], "sam")]
    # Survives a restart: a fresh StandingApprovals over the same store has it.
    assert StandingApprovals(store=store).get(rule["rule_id"]).created_by == "sam"


def test_list_rules_marks_origin_and_expiry(client, store, clock):
    rec = proposal(store)
    created = approve(client, rec, {"scope": "until", "until": (T0 + timedelta(hours=1)).isoformat()}).json()
    rule_id = created["standing_rule"]["rule_id"]

    rules = client.get("/api/approvals/rules").json()
    assert rules["count"] == 2
    by_id = {r["rule_id"]: r for r in rules["rules"]}
    assert by_id["file-backups"]["origin"] == "file" and by_id["file-backups"]["expires_at"] is None
    assert by_id[rule_id]["origin"] == "api" and by_id[rule_id]["created_by"] == "sam"
    assert by_id[rule_id]["expired"] is False

    clock.advance(hours=2)  # the app's clock, not the wall clock
    by_id = {r["rule_id"]: r for r in client.get("/api/approvals/rules").json()["rules"]}
    assert by_id[rule_id]["expired"] is True


@pytest.mark.parametrize(
    ("fields", "reason"),
    [({"impact": "high"}, "high impact"), ({"severity": Severity.CRITICAL}, "critical signal")],
)
def test_guardrail_refusal_approves_nothing(client, store, fields, reason):
    """A standing rule for a guarded proposal is a 422 with the reason, and the manual approval isn't applied."""
    rec = proposal(store, **fields)
    response = approve(client, rec, {"scope": "analyzer"})
    assert response.status_code == 422
    assert reason in response.json()["detail"] and "nothing was approved" in response.json()["detail"]
    after = store.get_recommendation(rec.id)
    assert after.status is Status.PROPOSED and after.revision == rec.revision
    assert [t.to_status for t in store.recommendation_history(rec.id)] == [Status.DRAFT, Status.PROPOSED]
    assert store.load_runtime_rules() == []
    assert [r.origin for r in runtime_of(client).approvals.rules()] == ["file"]
    # Without standing, the person can still approve it by hand.
    assert approve(client, rec).status_code == 200


def test_a_failing_rule_rolls_the_approval_back(client, store, monkeypatch):
    """The decision and the rule are one transaction: a rule that fails to save leaves the proposal untouched."""
    rec = proposal(store)

    def broken(rule):
        raise RuntimeError("disk full")

    monkeypatch.setattr(store, "save_runtime_rule", broken)
    with pytest.raises(RuntimeError, match="disk full"):
        approve(client, rec, {"scope": "analyzer"})
    assert store.get_recommendation(rec.id).status is Status.PROPOSED
    assert [r.origin for r in runtime_of(client).approvals.rules()] == ["file"]


def test_a_rollback_after_the_rule_was_added_forgets_it(client, store, monkeypatch):
    """Should the commit fail after the rule was saved, the in-memory copy is dropped as well."""
    rec = proposal(store)
    approvals = runtime_of(client).approvals
    real_transaction = store.transaction

    @contextmanager
    def failing_commit():
        with real_transaction():
            yield
            raise RuntimeError("commit failed")  # everything in the body succeeded; the commit doesn't

    monkeypatch.setattr(store, "transaction", failing_commit)
    with pytest.raises(RuntimeError, match="commit failed"):
        approve(client, rec, {"scope": "analyzer"})
    assert store.get_recommendation(rec.id).status is Status.PROPOSED
    assert store.load_runtime_rules() == []
    assert [r.origin for r in approvals.rules()] == ["file"]


@pytest.mark.parametrize(
    "standing",
    [
        {"scope": "until"},  # until needs a date
        {"scope": "once"},  # a plain approve: omit standing
        {"scope": "analyzer", "max_impact": "high"},
        {"scope": "analyzer", "title_pattern": "("},
        {"scope": "analyzer", "until": "2026-03-08T12:00:00"},  # naive
        {"scope": "analyzer", "until": (T0 - timedelta(minutes=1)).isoformat()},  # already past
        {"scope": "analyzer", "extra": 1},
    ],
)
def test_bad_standing_bodies_change_nothing(client, store, standing):
    rec = proposal(store)
    assert approve(client, rec, standing).status_code == 422
    assert store.get_recommendation(rec.id).status is Status.PROPOSED
    assert store.load_runtime_rules() == []


def test_standing_needs_the_viewed_revision_and_a_proposal(client, store):
    rec = proposal(store)
    response = approve(client, rec, {"scope": "analyzer"}, revision=rec.revision + 1)
    assert response.status_code == 409 and "changed since you viewed it" in response.json()["detail"]
    assert approve(client, rec).status_code == 200
    rec = store.get_recommendation(rec.id)
    assert approve(client, rec, {"scope": "analyzer"}).status_code == 409  # already approved
    assert store.load_runtime_rules() == []


def test_reject_takes_no_standing(client, store):
    rec = proposal(store)
    payload = {"by": "sam", "revision": rec.revision, "standing": {"scope": "analyzer"}}
    assert client.post(f"/api/recommendations/{rec.id}/reject", headers=AUTH, json=payload).status_code == 422
    assert store.get_recommendation(rec.id).status is Status.PROPOSED


# --- DELETE /api/approvals/rules/{rule_id} -------------------------------------------------------------------


def test_delete_a_runtime_rule(client, store):
    rule_id = approve(client, proposal(store), {"scope": "analyzer"}).json()["standing_rule"]["rule_id"]
    response = client.request("DELETE", f"/api/approvals/rules/{rule_id}", headers=AUTH, json={"by": "alex"})
    assert response.status_code == 200
    assert response.json() == {"rule_id": rule_id, "deleted": True, "by": "alex"}
    assert store.load_runtime_rules() == []
    assert [r["rule_id"] for r in client.get("/api/approvals/rules").json()["rules"]] == ["file-backups"]
    again = client.request("DELETE", f"/api/approvals/rules/{rule_id}", headers=AUTH, json={"by": "alex"})
    assert again.status_code == 404


def test_delete_a_file_rule_is_409(client):
    response = client.request("DELETE", "/api/approvals/rules/file-backups", headers=AUTH, json={"by": "alex"})
    assert response.status_code == 409 and "edit the approvals file" in response.json()["detail"]
    assert [r["rule_id"] for r in client.get("/api/approvals/rules").json()["rules"]] == ["file-backups"]


def test_delete_unknown_is_404_and_needs_by(client):
    assert client.request("DELETE", "/api/approvals/rules/api-nope", headers=AUTH, json={"by": "a"}).status_code == 404
    assert client.request("DELETE", "/api/approvals/rules/api-nope", headers=AUTH, json={}).status_code == 422


# --- the rule at work ----------------------------------------------------------------------------------------


def test_a_rule_created_over_http_auto_approves_the_next_match_and_honours_expiry(client, store, clock):
    """The analysis cycle the scheduler runs shares the API's StandingApprovals: a rule added over HTTP approves
    the next matching proposal in the next cycle, only to ``approved``, and stops at its expiry."""
    cycle = runtime_of(client).scheduler.jobs["analysis"].func
    first = raised(cycle.engine.run_cycle(ran(hit())))
    assert first.status is Status.PROPOSED

    standing = {"scope": "until", "until": (T0 + timedelta(hours=3)).isoformat(), "title_pattern": r"is \d+% full"}
    rule_id = approve(client, first, standing).json()["standing_rule"]["rule_id"]

    clock.advance(hours=1)
    second = raised(cycle.engine.run_cycle(ran(hit(KEY2, title="/srv on node-a:9100 is 91% full"))))
    stored = store.get_recommendation(second.id)
    assert stored.status is Status.APPROVED  # never further
    assert stored.approval_source == "standing" and stored.approved_by_rule == rule_id

    shown = client.get(f"/api/recommendations/{second.id}").json()
    assert shown["approved_by_rule"] == rule_id and shown["history"][-1]["rule_id"] == rule_id
    assert f"approved by standing rule {rule_id}" in client.get("/wall").text

    clock.advance(hours=3)  # past the rule's expiry
    third = raised(cycle.engine.run_cycle(ran(hit(KEY3, title="/var on node-a:9100 is 92% full"))))
    assert store.get_recommendation(third.id).status is Status.PROPOSED
    assert not [r for r in store.list_recommendations() if r.status is Status.DEPLOYED]


def test_a_deleted_rule_stops_approving(client, store, clock):
    cycle = runtime_of(client).scheduler.jobs["analysis"].func
    first = raised(cycle.engine.run_cycle(ran(hit())))
    rule_id = approve(client, first, {"scope": "analyzer", "title_pattern": "full"}).json()["standing_rule"]["rule_id"]
    client.request("DELETE", f"/api/approvals/rules/{rule_id}", headers=AUTH, json={"by": "sam"})
    clock.advance(minutes=5)
    second = raised(cycle.engine.run_cycle(ran(hit(KEY2, title="/srv is full"))))
    assert store.get_recommendation(second.id).status is Status.PROPOSED
