from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from argus_scout.events import SUMMARY_MAX, StatusEvent
from argus_scout.inbox import Inbox, InvalidEvent, Outcome, parse_envelope
from argus_scout.severity import Severity
from argus_scout.store import SqliteStore

T0 = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def inbox(clock):
    store = SqliteStore(":memory:")
    yield Inbox(store, clock=clock)
    store.close()


def env(event_id="disk-1", *, source="nas", severity="warning", summary="Disk 3 is 91% full", **extra):
    return {"event_id": event_id, "source": source, "severity": severity, "summary": summary, **extra}


def test_new_then_refreshed(inbox):
    first = inbox.ingest(env())
    assert first.outcome is Outcome.NEW and first.is_news
    again = inbox.ingest(env())
    assert again.outcome is Outcome.REFRESHED and not again.is_news
    assert again.previous_severity is Severity.WARNING
    assert [e.event_id for e in inbox.open()] == ["disk-1"]


def test_escalation_and_update(inbox):
    inbox.ingest(env(severity="notice"))
    up = inbox.ingest(env(severity="critical"))
    assert up.outcome is Outcome.ESCALATED
    assert up.previous_severity is Severity.NOTICE
    down = inbox.ingest(env(severity="warning"))
    assert down.outcome is Outcome.UPDATED
    reworded = inbox.ingest(env(severity="warning", summary="Disk 3 is 95% full"))
    assert reworded.outcome is Outcome.UPDATED


def test_escalation_reopens_an_acked_event_but_a_refresh_does_not(inbox):
    inbox.ingest(env(severity="notice"))
    assert inbox.ack("nas", "disk-1", by="person")
    assert inbox.open() == []
    refreshed = inbox.ingest(env(severity="notice"))
    assert refreshed.outcome is Outcome.REFRESHED and not refreshed.reopened
    assert inbox.open() == []
    escalated = inbox.ingest(env(severity="critical"))
    assert escalated.reopened
    assert [e.severity for e in inbox.open()] == [Severity.CRITICAL]


def test_duplicate_of_fingerprint(inbox):
    inbox.ingest(env("a"))
    dup = inbox.ingest(env("b", summary="  disk 3 IS 91%   full "))
    assert dup.outcome is Outcome.DUPLICATE
    assert dup.duplicate_of == (("nas", "a"),)
    # A different source saying the same thing is not a duplicate: the fingerprint includes the source.
    assert inbox.ingest(env("c", source="backup")).outcome is Outcome.NEW


def test_acked_or_expired_events_are_not_duplicated(inbox, clock):
    inbox.ingest(env("a", expires_at=(T0 + timedelta(minutes=5)).isoformat()))
    inbox.ingest(env("x", summary="other"))
    inbox.ack("nas", "x", by="person")
    clock.now = T0 + timedelta(minutes=10)
    assert inbox.ingest(env("b")).outcome is Outcome.NEW
    assert inbox.ingest(env("y", summary="other")).outcome is Outcome.NEW


def test_ack(inbox):
    inbox.ingest(env())
    with pytest.raises(ValueError, match="who"):
        inbox.ack("nas", "disk-1", by=" ")
    assert inbox.ack("nas", "disk-1", by="person")
    assert not inbox.ack("nas", "disk-1", by="person")
    assert not inbox.ack("nas", "missing", by="person")
    assert inbox.store.get_entry("nas", "disk-1").acked_by == "person"


def test_open_grouped_collapses_by_fingerprint(inbox, clock):
    inbox.ingest(env("a"))
    clock.now = T0 + timedelta(minutes=1)
    inbox.ingest(env("other", summary="UPS on battery", severity="critical"))
    inbox.ingest(env("b", generated_at=(T0 + timedelta(minutes=2)).isoformat()))
    groups = inbox.open_grouped()
    assert [g.count for g in groups] == [2, 1]
    assert [e.event_id for e in groups[0].events] == ["a", "b"]
    assert groups[0].first.event_id == "a" and groups[0].latest.event_id == "b"
    assert groups[1].fingerprint == "nas|critical|ups on battery"


def test_ingest_normalises(inbox, clock):
    raw = StatusEvent(
        event_id=" e1 ",
        source=" nas ",
        severity="LOUD",  # type: ignore[arg-type]
        generated_at=datetime(2026, 3, 1, 9, 0),
        summary="line one\n  line two " + "x" * 400,
        acked_at=T0,
        broadcast_count=9,
    )
    event = inbox.ingest(raw).event
    assert (event.source, event.event_id) == ("nas", "e1")
    assert event.severity is Severity.INFO
    assert event.generated_at.tzinfo is UTC
    assert len(event.summary) == SUMMARY_MAX and event.summary.startswith("line one line two x")
    assert event.acked_at is None and event.broadcast_count == 0  # only scout sets bookkeeping
    assert inbox.open()[0].event_id == "e1"


def test_ingest_refuses_malformed(inbox):
    with pytest.raises(InvalidEvent, match="summary: required"):
        inbox.ingest({"source": "nas"})
    with pytest.raises(InvalidEvent, match="source: required"):
        inbox.ingest({"summary": "hi"})
    assert inbox.open() == []


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ([], "expected an object, got list"),
        ({"source": "a", "summary": {"x": 1}}, "summary: expected text, got dict"),
        ({"source": "a", "summary": "s", "speakable": "maybe"}, "speakable: expected true or false"),
        ({"source": "a", "summary": "s", "generated_at": "yesterday"}, "generated_at: not an ISO-8601 date"),
        ({"source": "a", "summary": "s", "payload": [1]}, "payload: expected an object"),
        ({"source": "a", "summary": "s", "severity": 3}, "severity: expected text"),
        ({"source": "a", "summary": "s", "schema_version": "2"}, "schema_version: expected a positive integer"),
        ({"source": "a", "summary": "s", "event_id": "x" * 201}, "event_id: longer than 200"),
    ],
)
def test_parse_envelope_errors_name_the_field(raw, message):
    with pytest.raises(InvalidEvent, match=message):
        parse_envelope(raw)


def test_parse_envelope_coerces():
    event = parse_envelope(
        {"title": "Backup ok", "severity": "Warning", "speakable": "false", "requires_ack": 1, "generated_at": 0},
        default_source="ci",
    )
    assert event.source == "ci" and event.summary == "Backup ok"
    assert event.severity is Severity.WARNING
    assert event.speakable is False and event.requires_ack is True
    assert event.generated_at == datetime(1970, 1, 1, tzinfo=UTC)
    assert event.event_id.startswith("ci:")
    assert parse_envelope({"summary": "s"}, default_source="ci", default_event_id="fixed").event_id == "fixed"
