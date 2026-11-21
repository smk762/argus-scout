from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime, timedelta, timezone

import pytest

from argus_scout.approvals import StandingApprovals
from argus_scout.events import StatusEvent
from argus_scout.heartbeat import HeartbeatPolicy, reannounce_due
from argus_scout.recommendations import Confidence, Recommendation, Status
from argus_scout.severity import Severity
from argus_scout.store import SqliteStore, Store
from argus_scout.store.sqlite import MIGRATIONS, SCHEMA_VERSION, migrate

T0 = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)


def event(event_id="disk-1", *, source="nas", severity="warning", summary="Disk 3 is 91% full", **extra) -> StatusEvent:
    return StatusEvent.from_dict(
        {
            "event_id": event_id,
            "source": source,
            "severity": severity,
            "summary": summary,
            "generated_at": T0.isoformat(),
            **extra,
        }
    )


def proposal(**extra) -> Recommendation:
    fields = {
        "title": "Defer backup to off-peak",
        "action": "Move the job to 02:00",
        "impact": "low savings",
        "analyzer": "power",
        "severity": Severity.WARNING,
        "status": Status.PROPOSED,
    }
    return Recommendation(**{**fields, **extra})


@pytest.fixture
def store():
    with SqliteStore(":memory:") as s:
        yield s


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "data" / "scout.db"


def test_satisfies_protocol(store):
    assert isinstance(store, Store)


# -- schema ----------------------------------------------------------------------


def test_file_store_uses_wal_and_foreign_keys(db_path):
    with SqliteStore(db_path) as s:
        assert s._conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert s._conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert s.schema_version == SCHEMA_VERSION


def test_migrations_are_idempotent(db_path):
    SqliteStore(db_path).close()
    with SqliteStore(db_path) as s:
        assert s.schema_version == SCHEMA_VERSION
        assert migrate(s._conn) == SCHEMA_VERSION
        tables = {r[0] for r in s._conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"events", "recommendations", "recommendation_transitions", "standing_rules", "reports"} <= tables


def test_newer_schema_is_refused(db_path):
    SqliteStore(db_path).close()
    conn = sqlite3.connect(db_path)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    conn.close()
    with pytest.raises(RuntimeError, match="newer"):
        SqliteStore(db_path)


# -- events ------------------------------------------------------------------------


def test_event_round_trip(store):
    original = event(body_md="details", requires_ack=True, link="https://example.invalid/x", payload={"pct": 91})
    assert store.upsert_event(original, now=T0) is True
    got = store.get_event("nas", "disk-1")
    assert got is not None
    for key in ("event_id", "source", "severity", "summary", "body_md", "requires_ack", "link", "payload"):
        assert getattr(got, key) == getattr(original, key)
    assert got.generated_at == T0
    assert got.generated_at.tzinfo is not None
    assert store.get_event("nas", "nope") is None


def test_upsert_updates_last_seen_without_duplicating(store):
    assert store.upsert_event(event(), now=T0) is True
    later = T0 + timedelta(minutes=5)
    assert store.upsert_event(event(body_md="still full"), now=later) is False
    entry = store.get_entry("nas", "disk-1")
    assert entry.first_seen == T0
    assert entry.last_seen == later
    assert entry.seen_count == 2
    assert entry.event.body_md == "still full"
    assert entry.event.ingested_at == T0
    assert len(store.open_events()) == 1


def test_ack(store):
    store.upsert_event(event(), now=T0)
    assert store.ack_event("nas", "disk-1", "someone", at=T0 + timedelta(hours=1)) is True
    assert store.ack_event("nas", "disk-1", "someone") is False  # already acked
    assert store.ack_event("nas", "missing", "someone") is False
    entry = store.get_entry("nas", "disk-1")
    assert entry.acked_by == "someone"
    assert entry.event.acked_at == T0 + timedelta(hours=1)
    assert store.open_events() == []


def test_unchanged_reupsert_keeps_ack_but_changed_content_reopens(store):
    store.upsert_event(event(), now=T0)
    store.ack_event("nas", "disk-1", "someone")
    store.upsert_event(event(), now=T0 + timedelta(minutes=1))
    assert store.open_events() == []
    store.upsert_event(event(severity="critical"), now=T0 + timedelta(minutes=2))
    [reopened] = store.open_events()
    assert reopened.severity == Severity.CRITICAL
    assert store.get_entry("nas", "disk-1").acked_by is None


def test_open_events_skip_expired(store):
    store.upsert_event(event("a", expires_at=(T0 + timedelta(hours=1)).isoformat()), now=T0)
    store.upsert_event(event("b", summary="other"), now=T0)
    assert [e.event_id for e in store.open_events(now=T0)] == ["a", "b"]
    assert [e.event_id for e in store.open_events(now=T0 + timedelta(hours=2))] == ["b"]
    assert len(store.open_events(now=T0 + timedelta(hours=2), include_expired=True)) == 2


def test_fingerprint_dedup_query(store):
    # The same news re-emitted under fresh ids, plus one unrelated event.
    store.upsert_event(event("x1"), now=T0)
    store.upsert_event(event("x2", summary="  disk 3 is 91%   FULL "), now=T0 + timedelta(seconds=1))
    store.upsert_event(event("y1", summary="UPS on battery"), now=T0)
    fp = event().fingerprint
    assert [e.event_id for e in store.events_by_fingerprint(fp)] == ["x1", "x2"]
    assert store.open_fingerprints()[fp] == 2
    store.ack_event("nas", "x1", "someone")
    assert [e.event_id for e in store.events_by_fingerprint(fp)] == ["x2"]
    assert len(store.events_by_fingerprint(fp, open_only=False)) == 2
    plan = store._conn.execute("EXPLAIN QUERY PLAN SELECT * FROM events WHERE fingerprint = ?", (fp,)).fetchall()
    assert any("events_fingerprint" in str(tuple(r)) for r in plan)


def test_mark_announced_drives_reannounce(store):
    store.upsert_event(event(), now=T0)
    assert store.mark_announced("nas", "disk-1", at=T0) is True
    got = store.get_event("nas", "disk-1")
    assert got.last_broadcast_at == T0
    assert got.broadcast_count == 1
    interval = 3600
    policy = HeartbeatPolicy(reannounce_after={"warning": interval})
    assert not reannounce_due([got], policy, now=T0 + timedelta(seconds=interval - 1))
    assert reannounce_due([got], policy, now=T0 + timedelta(seconds=interval))


def test_datetimes_normalised_to_utc(store):
    local = datetime(2026, 3, 1, 14, 0, tzinfo=timezone(timedelta(hours=2)))
    store.upsert_event(event(), now=local)
    entry = store.get_entry("nas", "disk-1")
    assert entry.first_seen == T0
    assert entry.first_seen.tzinfo == UTC


# -- recommendations ------------------------------------------------------------------


def test_recommendation_round_trip(store):
    rec = proposal(evidence=["disk at 91%"], confidence=Confidence.HIGH, rationale="cheaper")
    store.save_recommendation(rec)
    got = store.get_recommendation(rec.id)
    assert got.to_dict() == rec.to_dict()
    assert store.get_recommendation("missing") is None


def test_list_by_status(store):
    a, b, c = proposal(title="a"), proposal(title="b"), proposal(title="c")
    b.approve()
    c.reject("no")
    for r in (a, b, c):
        store.save_recommendation(r)
    assert [r.title for r in store.list_recommendations(Status.PROPOSED)] == ["a"]
    assert {r.title for r in store.list_recommendations([Status.APPROVED, Status.REJECTED])} == {"b", "c"}
    assert len(store.list_recommendations()) == 3
    assert len(store.list_recommendations(limit=2)) == 2


def test_audit_trail_survives_restart(db_path):
    rec = proposal(status=Status.DRAFT)
    with SqliteStore(db_path) as s:
        s.save_recommendation(rec, by="analyzer:power")
        rec.transition(Status.PROPOSED)
        s.save_recommendation(rec)
        s.save_recommendation(rec)  # no status change, no new row
        rec.approve(rule_id="evening-shift")
        s.save_recommendation(rec)
        rec.transition(Status.DEPLOYED)
        s.save_recommendation(rec, by="a person", reason="done by hand")
    with SqliteStore(db_path) as s:
        got = s.get_recommendation(rec.id)
        history = s.recommendation_history(rec.id)
    assert got.status == Status.DEPLOYED
    assert got.approval_source == "standing"
    assert got.approved_by_rule == "evening-shift"
    assert got.approved_at == rec.approved_at
    assert [(t.from_status, t.to_status) for t in history] == [
        (None, Status.DRAFT),
        (Status.DRAFT, Status.PROPOSED),
        (Status.PROPOSED, Status.APPROVED),
        (Status.APPROVED, Status.DEPLOYED),
    ]
    approval = history[2]
    assert (approval.source, approval.rule_id, approval.by) == ("standing", "evening-shift", "rule:evening-shift")
    assert approval.at == rec.approved_at
    assert (history[3].by, history[3].reason) == ("a person", "done by hand")
    assert history[0].by == "analyzer:power"


def test_rejection_reason_in_trail(store):
    rec = proposal()
    store.save_recommendation(rec)
    rec.reject("too noisy")
    store.save_recommendation(rec, by="a person")
    last = store.recommendation_history(rec.id)[-1]
    assert (last.to_status, last.reason, last.by) == (Status.REJECTED, "too noisy", "a person")


def test_manual_approval_source(store):
    rec = proposal()
    rec.approve()
    store.save_recommendation(rec, by="a person")
    [t] = store.recommendation_history(rec.id)
    assert (t.source, t.rule_id, t.by) == ("manual", None, "a person")


# -- standing approvals ---------------------------------------------------------------


def test_runtime_rules_survive_restart(db_path):
    expiry = T0 + timedelta(days=30)
    with SqliteStore(db_path) as s:
        approvals = StandingApprovals(store=s)
        rule = approvals.add_from(proposal(), "until", expires_at=expiry)
        doomed = approvals.add_from(proposal(title="Other thing"), "analyzer")
        assert approvals.remove(doomed.rule_id)
    with SqliteStore(db_path) as s:
        approvals = StandingApprovals(store=s)
        [restored] = approvals.rules()
        assert restored.to_dict() == rule.to_dict()
        assert restored.origin == "api"
        assert restored.expires_at == expiry
        # The restored rule still approves (and only approves).
        rec = proposal()
        assert approvals.find_match(rec, now=T0) is not None


def test_runtime_rule_store_keeps_file_rules_out(store, write_file):
    path = write_file("approvals.yaml", "rules:\n  - id: from-file\n    analyzer: power\n")
    approvals = StandingApprovals(path, store=store)
    assert [r.rule_id for r in approvals.rules()] == ["from-file"]
    assert store.load_runtime_rules() == []
    with pytest.raises(ValueError, match="file rules"):
        store.save_runtime_rule(approvals.rules()[0])


def test_guardrail_refusal_stores_nothing(store):
    approvals = StandingApprovals(store=store)
    with pytest.raises(ValueError):
        approvals.add_from(proposal(severity=Severity.CRITICAL), "analyzer")
    assert store.load_runtime_rules() == []


# -- reports ---------------------------------------------------------------------------


def test_reports(store):
    store.add_report("morning", T0, "Good morning.", {"model": "small", "tokens": 120})
    store.add_report("status", T0 + timedelta(hours=1), "All quiet.", {})
    newest = store.add_report("morning", T0 + timedelta(days=1), "Morning again.", None)
    assert newest.id > 0
    assert store.latest_report().text == "Morning again."
    assert store.latest_report("status").text == "All quiet."
    assert store.latest_report("evening") is None
    first = store.list_reports(kind="morning")[-1]
    assert (first.meta, first.generated_at) == ({"model": "small", "tokens": 120}, T0)
    assert [r.kind for r in store.list_reports(2)] == ["morning", "status"]


# -- retention and restarts ---------------------------------------------------------------


def test_prune(store):
    store.upsert_event(event("old-acked"), now=T0)
    store.ack_event("nas", "old-acked", "someone", at=T0)
    store.upsert_event(event("new-acked", summary="b"), now=T0)
    store.ack_event("nas", "new-acked", "someone", at=T0 + timedelta(days=10))
    store.upsert_event(event("open", summary="c"), now=T0)
    store.upsert_event(event("expired", summary="d", expires_at=T0.isoformat()), now=T0)
    store.add_report("morning", T0, "old")
    store.add_report("morning", T0 + timedelta(days=10), "new")
    rec = proposal()
    store.save_recommendation(rec)

    assert store.prune(T0 + timedelta(days=5)) == {"events": 2, "reports": 1, "deliveries": 0}
    assert store.get_event("nas", "old-acked") is None
    assert store.get_event("nas", "expired") is None
    assert store.get_event("nas", "new-acked") is not None
    assert store.get_event("nas", "open") is not None
    assert [r.text for r in store.list_reports()] == ["new"]
    assert store.get_recommendation(rec.id) is not None


def test_restart_keeps_everything(db_path):
    rec = proposal()
    with SqliteStore(db_path) as s:
        s.upsert_event(event(), now=T0)
        s.ack_event("nas", "disk-1", "someone", at=T0)
        s.upsert_event(event("open", summary="still open"), now=T0)
        s.save_recommendation(rec)
        s.add_report("morning", T0, "hello", {"k": 1})
    with SqliteStore(db_path) as s:
        assert s.get_entry("nas", "disk-1").acked_by == "someone"
        assert [e.event_id for e in s.open_events(now=T0)] == ["open"]
        assert s.get_recommendation(rec.id).to_dict() == rec.to_dict()
        assert s.latest_report("morning").meta == {"k": 1}


def test_shared_across_threads(db_path):
    with SqliteStore(db_path) as s:
        errors: list[Exception] = []

        def work(n: int) -> None:
            try:
                for i in range(25):
                    s.upsert_event(event(f"t{n}-{i}", summary=f"thread {n} item {i}"))
                    s.add_report("status", T0, f"{n}-{i}")
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=work, args=(n,)) for n in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []
        assert len(s.open_events()) == 100
        assert len(s.list_reports(1000)) == 100


def test_key_migration_keeps_old_rows(tmp_path):
    """A database from before the dedup key: its rows load, with an empty key, and new rows are found by key."""
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.executescript(MIGRATIONS[0] + MIGRATIONS[1] + "PRAGMA user_version = 2;")
    old = proposal().to_dict()
    for name in ("key", "expires_at", "last_seen_at", "retired_reason"):
        old.pop(name)
    conn.execute(
        "INSERT INTO recommendations (id, status, analyzer, created_at, updated_at, data) VALUES (?, ?, ?, ?, ?, ?)",
        (old["id"], old["status"], old["analyzer"], old["created_at"], old["updated_at"], json.dumps(old)),
    )
    conn.commit()
    conn.close()
    with SqliteStore(db) as s:
        assert s.schema_version == SCHEMA_VERSION
        [got] = s.list_recommendations()
        assert got.key == "" and got.expires_at is None
        keyed = proposal(key='disk{instance="a"}')
        s.save_recommendation(keyed)
        assert [r.id for r in s.recommendations_by_key(keyed.key, Status.PROPOSED)] == [keyed.id]
        assert s.recommendations_by_key(keyed.key, Status.APPROVED) == []


def test_copy_of_is_a_detached_snapshot(tmp_path):
    db = tmp_path / "scout.db"
    with SqliteStore(db) as s:
        s.save_recommendation(proposal())
    copy = SqliteStore.copy_of(db)
    copy.save_recommendation(proposal())
    assert len(copy.list_recommendations()) == 2
    copy.close()
    with SqliteStore(db) as s:
        assert len(s.list_recommendations()) == 1
    assert SqliteStore.copy_of(tmp_path / "missing.db").list_recommendations() == []
    assert not (tmp_path / "missing.db").exists()


def test_signal_pending_round_trip(tmp_path):
    with SqliteStore(tmp_path / "p.db") as store:
        store.save_signal_pending("r", {"k": T0})
        store.save_signal_pending("gone", {"k": T0})
        assert store.load_signal_pending() == {"r": {"k": T0}, "gone": {"k": T0}}
        assert store.drop_signal_pending(["r"]) == 1
        store.save_signal_pending("r", {})
        assert store.load_signal_pending() == {}


def test_resolved_column_migration_keeps_open_events_open(tmp_path):
    """A database from before ``resolved_at``: its events stay open, and resolving one never touches the ack."""
    db = tmp_path / "old.db"
    before = MIGRATIONS.index(next(m for m in MIGRATIONS if "resolved_at" in m))
    with SqliteStore(db) as s:
        pass
    conn = sqlite3.connect(db)
    conn.execute("DROP TABLE events")  # rebuild at the pre-resolved_at schema
    conn.executescript(MIGRATIONS[0].split("CREATE TABLE recommendations")[0])
    conn.execute(f"PRAGMA user_version = {before}")
    conn.commit()
    conn.close()
    with SqliteStore(db) as s:
        assert s.schema_version == SCHEMA_VERSION
    with SqliteStore(db) as s:
        s.upsert_event(event("a"), now=T0)
        s.upsert_event(event("b", summary="other"), now=T0)
        assert s.resolve_missing("nas", ["b"], at=T0) == ["a"]
        assert s.resolve_missing("nas", ["b"], at=T0) == []  # idempotent
        assert [e.event_id for e in s.open_events(now=T0)] == ["b"]
        assert s.get_entry("nas", "a").acked_by is None
        assert s.events_by_fingerprint(event("a").fingerprint) == []
        assert s.prune(T0 + timedelta(seconds=1))["events"] == 1
        assert s.get_entry("nas", "a") is None and s.get_entry("nas", "b") is not None
