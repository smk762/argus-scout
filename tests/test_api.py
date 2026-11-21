"""The HTTP API's routes: success and refusal paths. The guards themselves are tested in test_api_guards.py."""

from __future__ import annotations

from datetime import timedelta

import pytest
from api_helpers import AUTH, NOW, OWN_TOKEN, TOKEN, make_app
from fastapi.testclient import TestClient

from argus_scout.events import StatusEvent
from argus_scout.recommendations import Recommendation, Status
from argus_scout.server.guards import iter_routes
from argus_scout.severity import Severity
from argus_scout.store import SqliteStore


@pytest.fixture(autouse=True)
def tokens(monkeypatch):
    monkeypatch.setenv("SCOUT_API_TOKEN", TOKEN)
    monkeypatch.setenv("OWN_TOKEN", OWN_TOKEN)


@pytest.fixture
def store():
    with SqliteStore(":memory:") as s:
        yield s


@pytest.fixture
def client(store, tmp_path):
    with TestClient(make_app(store, tmp_path)) as c:
        yield c


def proposal(store: SqliteStore, title: str = "Move the backup to off-peak", **kw) -> Recommendation:
    rec = Recommendation(title=title, action="reschedule", impact="low", analyzer="power", **kw)
    store.save_recommendation(rec)
    rec.transition(Status.PROPOSED)
    store.save_recommendation(rec, by="analyzer")
    return rec


# --- POST /api/events/{source} -------------------------------------------------------------------------------


def test_push_one_event_and_see_the_outcomes(client):
    event = {"event_id": "disk", "severity": "warning", "summary": "Disk 90% full"}
    response = client.post("/api/events/hook", headers=AUTH, json=event)
    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "hook" and body["accepted"] == 1
    assert body["events"][0] | {"duplicate_of": []} == {
        "event_id": "disk",
        "outcome": "new",
        "news": True,
        "reopened": False,
        "previous_severity": None,
        "duplicate_of": [],
    }

    again = client.post("/api/events/hook", headers=AUTH, json=event).json()["events"][0]
    assert again["outcome"] == "refreshed" and again["news"] is False

    worse = client.post("/api/events/hook", headers=AUTH, json={**event, "severity": "critical"}).json()["events"][0]
    assert worse["outcome"] == "escalated" and worse["previous_severity"] == "warning"


def test_push_a_batch_reports_duplicates(client):
    batch = [
        {"event_id": "a", "summary": "UPS on battery", "severity": "warning"},
        {"event_id": "b", "summary": "UPS on battery", "severity": "warning"},
    ]
    events = client.post("/api/events/hook", headers=AUTH, json=batch).json()["events"]
    assert [e["outcome"] for e in events] == ["new", "duplicate"]
    assert events[1]["duplicate_of"] == [{"source": "hook", "event_id": "a"}]


def test_push_with_the_sources_own_token(client):
    event = {"summary": "hello"}
    assert (
        client.post("/api/events/own", headers={"Authorization": f"Bearer {OWN_TOKEN}"}, json=event).status_code == 200
    )
    # A source with its own token doesn't accept the server token...
    assert client.post("/api/events/own", headers=AUTH, json=event).status_code == 401
    # ...and its token is good for that source only: not another push source, not the other write routes.
    own = {"Authorization": f"Bearer {OWN_TOKEN}"}
    assert client.post("/api/events/hook", headers=own, json=event).status_code == 401
    assert client.post("/api/recommendations/x/approve", headers=own, json={"by": "me"}).status_code == 401


def test_push_source_token_unset_is_503(client, monkeypatch):
    monkeypatch.delenv("OWN_TOKEN")
    response = client.post("/api/events/own", headers=AUTH, json={"summary": "x"})
    assert response.status_code == 503  # its own token is unset: closed, not open and not the server token


def test_push_unknown_or_polling_source(client):
    assert client.post("/api/events/nope", headers=AUTH, json={"summary": "x"}).status_code == 404
    response = client.post("/api/events/poller", headers=AUTH, json={"summary": "x"})
    assert response.status_code == 405
    assert "json_poll" in response.json()["detail"]


@pytest.mark.parametrize(
    ("body", "fragment"),
    [
        (b"{not json", "not JSON"),
        (b'"a string"', "event object or a list"),
        (b'{"severity": "warning"}', "summary: required"),
        (b'{"summary": "x", "source": "other"}', "does not match webhook source"),
        (b'[{"summary": "ok"}, {"summary": 3, "payload": []}]', "event 1: payload"),
    ],
)
def test_push_refused_bodies_ingest_nothing(client, store, body, fragment):
    response = client.post("/api/events/hook", headers={**AUTH, "Content-Type": "application/json"}, content=body)
    assert response.status_code == 422
    assert fragment in response.json()["detail"]
    assert store.open_events(now=NOW) == []  # all or nothing


def test_push_too_many_events(client):
    batch = [{"summary": f"e{i}"} for i in range(101)]
    assert client.post("/api/events/hook", headers=AUTH, json=batch).status_code == 422


# --- ack -----------------------------------------------------------------------------------------------------


def test_ack_an_event(client, store):
    client.post("/api/events/hook", headers=AUTH, json={"event_id": "ups/1", "summary": "UPS on battery"})
    response = client.post("/api/events/hook/ups/1/ack", headers=AUTH, json={"by": "  sam  "})
    assert response.status_code == 200
    assert response.json() == {"source": "hook", "event_id": "ups/1", "acked": True, "by": "sam", "at": NOW.isoformat()}
    assert store.get_entry("hook", "ups/1").acked_by == "sam"
    assert client.get("/api/events").json()["count"] == 0

    assert client.post("/api/events/hook/ups/1/ack", headers=AUTH, json={"by": "sam"}).status_code == 409
    assert client.post("/api/events/hook/nope/ack", headers=AUTH, json={"by": "sam"}).status_code == 404


@pytest.mark.parametrize(
    "body",
    [b"", b"{}", b'{"by": ""}', b'{"by": "   "}', b'{"by": "x\\u001b[31m"}', b'{"by": "sam", "extra": 1}'],
)
def test_ack_needs_a_valid_by(client, body):
    client.post("/api/events/hook", headers=AUTH, json={"event_id": "e", "summary": "s"})
    response = client.post("/api/events/hook/e/ack", headers={**AUTH, "Content-Type": "application/json"}, content=body)
    assert response.status_code == 422


def test_ack_bad_json_is_400(client):
    assert client.post("/api/events/hook/e/ack", headers=AUTH, content=b"{nope").status_code == 400


# --- approve / reject ----------------------------------------------------------------------------------------


def test_manual_approve_is_audited(client, store):
    rec = proposal(store)
    response = client.post(f"/api/recommendations/{rec.id}/approve", headers=AUTH, json={"by": "sam", "reason": "ok"})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "approved"
    assert body["approval_source"] == "manual" and body["approved_by_rule"] is None
    assert body["history"][-1] | {"at": None} == {
        "from": "proposed",
        "to": "approved",
        "at": None,
        "by": "sam",
        "source": "manual",
        "rule_id": None,
        "reason": "ok",
    }
    stored = store.get_recommendation(rec.id)
    assert stored.status is Status.APPROVED and stored.approval_source == "manual"


def test_standing_and_manual_approvals_are_told_apart(client, store):
    manual, standing = proposal(store, "manual one"), proposal(store, "standing one")
    standing.approve(rule_id="low-impact")
    store.save_recommendation(standing)
    client.post(f"/api/recommendations/{manual.id}/approve", headers=AUTH, json={"by": "sam"})

    def last(rec_id):
        return client.get(f"/api/recommendations/{rec_id}").json()["history"][-1]

    assert (last(manual.id)["source"], last(manual.id)["by"], last(manual.id)["rule_id"]) == ("manual", "sam", None)
    assert (last(standing.id)["source"], last(standing.id)["by"], last(standing.id)["rule_id"]) == (
        "standing",
        "rule:low-impact",
        "low-impact",
    )
    assert store.approval_counts() == {"manual": 1, "standing": 1}


def test_reject_records_who_and_why(client, store):
    rec = proposal(store)
    response = client.post(
        f"/api/recommendations/{rec.id}/reject", headers=AUTH, json={"by": "sam", "reason": "backups run at night"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "rejected" and body["rejection_reason"] == "backups run at night"
    assert body["history"][-1]["by"] == "sam" and body["history"][-1]["reason"] == "backups run at night"
    assert body["approval_source"] is None


def test_only_a_proposal_can_be_decided(client, store):
    rec = proposal(store)
    assert client.post(f"/api/recommendations/{rec.id}/approve", headers=AUTH, json={"by": "a"}).status_code == 200
    for verb in ("approve", "reject"):
        response = client.post(f"/api/recommendations/{rec.id}/{verb}", headers=AUTH, json={"by": "b"})
        assert response.status_code == 409
        assert "is approved" in response.json()["detail"]
    draft = Recommendation(title="draft")
    store.save_recommendation(draft)
    assert client.post(f"/api/recommendations/{draft.id}/approve", headers=AUTH, json={"by": "a"}).status_code == 409
    assert client.post("/api/recommendations/nope/approve", headers=AUTH, json={"by": "a"}).status_code == 404
    assert store.get_recommendation(rec.id).status is Status.APPROVED  # nothing moved past approved


def test_no_route_moves_a_recommendation_past_approved(client):
    paths = {view.path for view in iter_routes(client.app)}
    assert "/api/recommendations/{rec_id}/approve" in paths
    assert not [p for p in paths if any(word in p for word in ("deploy", "retire", "execute", "dispatch"))]


def test_decision_body_validation(client, store):
    rec = proposal(store)
    url = f"/api/recommendations/{rec.id}/reject"
    assert client.post(url, headers=AUTH, json={"reason": "no who"}).status_code == 422
    assert client.post(url, headers=AUTH, json={"by": "sam", "reason": "x" * 2001}).status_code == 422
    assert client.post(url, headers=AUTH, json={"by": "sam", "reason": "line one\nline two"}).status_code == 200


# --- reads ---------------------------------------------------------------------------------------------------


def test_list_recommendations_by_status(client, store):
    a, b = proposal(store, "a"), proposal(store, "b")
    client.post(f"/api/recommendations/{a.id}/approve", headers=AUTH, json={"by": "sam"})
    assert client.get("/api/recommendations").json()["count"] == 2
    approved = client.get("/api/recommendations?status=approved").json()["recommendations"]
    assert [r["id"] for r in approved] == [a.id]
    both = client.get("/api/recommendations?status=approved&status=proposed").json()
    assert {r["id"] for r in both["recommendations"]} == {a.id, b.id}
    assert client.get("/api/recommendations?status=bogus").status_code == 422
    assert client.get("/api/recommendations?limit=0").status_code == 422
    assert client.get("/api/recommendations/nope").status_code == 404


def test_open_events_grouped(client):
    client.post(
        "/api/events/hook",
        headers=AUTH,
        json=[{"event_id": i, "summary": "UPS on battery", "severity": "warning"} for i in ("a", "b")]
        + [{"event_id": "c", "summary": "Disk full", "severity": "critical"}],
    )
    body = client.get("/api/events").json()
    assert body["count"] == 3
    assert [(g["summary"], g["count"], g["event_ids"]) for g in body["groups"]] == [
        ("UPS on battery", 2, ["a", "b"]),
        ("Disk full", 1, ["c"]),
    ]


def test_latest_report(client, store):
    assert client.get("/api/reports/latest").status_code == 404
    store.add_report("morning", NOW - timedelta(hours=4), "Good morning.")
    store.add_report("status", NOW, "All quiet.")
    assert client.get("/api/reports/latest").json()["text"] == "All quiet."
    assert client.get("/api/reports/latest?kind=morning").json()["kind"] == "morning"
    assert client.get("/api/reports/latest?kind=evening").status_code == 404


# --- explain and the wall, on a seeded store -----------------------------------------------------------------


@pytest.fixture
def seeded(store, write_file, tmp_path):
    policy = write_file(
        "heartbeat.yaml",
        "report: {min_severity: warning}\nsources: {chatty: {channel: briefing}}\n",
    )
    for event_id, source, severity, summary in [
        ("disk", "nas", "critical", "Disk 95% full"),
        ("temp", "nas", "notice", "Warm in the cupboard"),
        ("news", "chatty", "warning", "Something chatty"),
    ]:
        store.upsert_event(
            StatusEvent(
                event_id=event_id,
                source=source,
                severity=Severity(severity),
                generated_at=NOW,
                summary=summary,
                speakable=source != "nas" or severity == "critical",
            ),
            now=NOW,
        )
    store.add_report("morning", NOW - timedelta(hours=5), "Good morning.\nAll systems nominal.\x1b[2J")
    proposal(store, "Shift the backup to off-peak", severity=Severity.NOTICE)
    return policy


def seeded_client(store, tmp_path, policy):
    from argus_scout.config import parse_config
    from argus_scout.server.app import create_app

    config = parse_config({"files": {"heartbeat": str(policy)}, "schedule": {"timezone": "UTC"}}, tmp_path)
    return TestClient(create_app(config, store=store, clock=lambda: NOW, run_scheduler=False))


def test_policy_explain(store, tmp_path, seeded):
    with seeded_client(store, tmp_path, seeded) as client:
        body = client.get("/api/policy/explain").json()
    assert body["now"] == NOW.isoformat() and body["resting"] is False and body["quiet"] is False
    assert body["policy"]["file_loaded"] is True
    assert body["policy"]["origin"]["report.min_severity"] == "file"
    assert body["policy"]["origin"]["voice.min_severity"] == "default"
    items = {i["event_id"]: i for i in body["items"]}
    assert items["disk"] | {"fingerprint": None} == {
        "event_id": "disk",
        "source": "nas",
        "severity": "critical",
        "broadcast": True,
        "report": True,
        "voice": True,
        "rule": "report.min_severity=warning; voice.min_severity=warning",
        "summary": "Disk 95% full",
        "count": 1,
        "fingerprint": None,
    }
    assert (items["temp"]["report"], items["temp"]["rule"]) == (False, "report.min_severity=warning")
    assert (items["news"]["report"], items["news"]["rule"]) == (False, "sources.chatty.channel=briefing")


def test_policy_explain_in_rest_hours(store, tmp_path, write_file):
    policy = write_file("heartbeat.yaml", "rest_hours: {start: '11:00', end: '13:00'}\n")
    store.upsert_event(
        StatusEvent(event_id="x", source="nas", severity=Severity.CRITICAL, generated_at=NOW, summary="fire"), now=NOW
    )
    with seeded_client(store, tmp_path, policy) as client:
        body = client.get("/api/policy/explain").json()
    assert body["resting"] is True
    item = body["items"][0]
    assert (item["broadcast"], item["report"], item["voice"]) == (False, False, False)
    assert item["rule"].startswith("rest_hours; ")


def test_policy_explain_with_a_broken_file_uses_defaults(store, tmp_path, write_file):
    policy = write_file("heartbeat.yaml", "report: [unclosed\n")
    with seeded_client(store, tmp_path, policy) as client:
        body = client.get("/api/policy/explain").json()
    assert body["policy"]["file_error"]
    assert set(body["policy"]["origin"].values()) == {"default"}


def test_wall(store, tmp_path, seeded):
    with seeded_client(store, tmp_path, seeded) as client:
        response = client.get("/wall")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    text = response.text
    assert "\x1b" not in text  # producer/model text can't drive the terminal
    assert text.startswith("argus-scout  2026-06-01 12:00 UTC\n")
    assert "Latest report: morning, 2026-06-01 07:00" in text
    assert "Good morning.\nAll systems nominal." in text
    assert "Open items: 3" in text
    assert "  CRITICAL nas: Disk 95% full" in text
    assert "Awaiting a decision: 1" in text
    assert "Shift the backup to off-peak" in text
    assert all(len(line) <= 100 for line in text.splitlines())


def test_wall_when_empty(client):
    text = client.get("/wall").text
    assert "No report yet." in text and "Nothing open." in text and "Nothing to decide." in text
