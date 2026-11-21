"""The status inbox: every status event, from every source, lands here.

:class:`Inbox` holds the inbox logic on top of a :class:`~argus_scout.store.Store`:

- :meth:`Inbox.ingest` validates and normalises an envelope, upserts it by ``(source, event_id)``
  and says what it was (:class:`Outcome`): new, a duplicate of an open event with the same
  fingerprint, an escalation, an update, or a re-sighting of something already known.
- :meth:`Inbox.ack` acknowledges one event (a person); :meth:`Inbox.resolve_missing` closes the events a
  polled source stopped reporting (scout: ``resolved_at``, never ``acked_by``); :meth:`Inbox.open` lists the
  unacked, unresolved, unexpired ones;
  :meth:`Inbox.open_grouped` collapses open events that share a fingerprint.

**It only records.** The inbox never moves a recommendation and never sends anything: deciding
whether an event is worth announcing is the heartbeat policy's job, and delivery is a channel's.

:func:`parse_envelope` is the one validator for envelopes that arrive as plain data (a webhook
body, a polled item). It is stricter than :meth:`StatusEvent.from_dict` (which trusts stored
data): wrong types and unparseable dates are refused with a message naming the field, rather
than coerced into something the producer didn't mean. Severity is the exception: an unknown
level is read as ``info``, so a producer with its own vocabulary still gets through.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

import structlog

from argus_scout.events import SCHEMA_VERSION, SUMMARY_MAX, StatusEvent, group_duplicates, parse_dt
from argus_scout.severity import Severity, coerce_severity, rank
from argus_scout.store.base import Store

log = structlog.get_logger(__name__)

ID_MAX = 200
_TRUE = {"true", "yes", "on", "1"}
_FALSE = {"false", "no", "off", "0", ""}


class InvalidEvent(ValueError):
    """An envelope that can't be accepted. The message names the field and what was wrong."""


class Outcome(StrEnum):
    NEW = "new"
    """First sighting, and no other open event says the same thing."""
    DUPLICATE = "duplicate"
    """First sighting of this id, but an open event already has the same fingerprint."""
    ESCALATED = "escalated"
    """A known event whose severity went up. It is reopened if it was acked."""
    UPDATED = "updated"
    """A known event whose content changed without escalating. It is reopened if it was acked."""
    REFRESHED = "refreshed"
    """A known event seen again unchanged (a poller's usual case). Ack and announce state are kept."""
    RECURRED = "recurred"
    """A known event that had been resolved (cleared at its source) is back. It is open again, as new news."""


@dataclass(frozen=True)
class IngestResult:
    outcome: Outcome
    event: StatusEvent
    previous_severity: Severity | None = None
    """The stored severity before this ingest, for a known event."""
    reopened: bool = False
    """The event had been acked and changed, so it is open again."""
    duplicate_of: tuple[tuple[str, str], ...] = ()
    """``(source, event_id)`` of the open events this one duplicates."""

    @property
    def is_news(self) -> bool:
        """Whether this is something not already in the inbox in this form."""
        return self.outcome in (Outcome.NEW, Outcome.ESCALATED, Outcome.UPDATED, Outcome.RECURRED)


@dataclass
class EventGroup:
    """Open events sharing one fingerprint, oldest first."""

    fingerprint: str
    events: list[StatusEvent] = field(default_factory=list)

    @property
    def first(self) -> StatusEvent:
        return self.events[0]

    @property
    def latest(self) -> StatusEvent:
        return max(self.events, key=lambda e: e.generated_at)

    @property
    def count(self) -> int:
        return len(self.events)


# --- envelope validation -------------------------------------------------------------------------------------


def _text(raw: Mapping[str, Any], key: str, *, limit: int | None = None) -> str | None:
    value = raw.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, str | int | float):
        raise InvalidEvent(f"{key}: expected text, got {type(value).__name__}")
    text = str(value).strip()
    if limit is not None and len(text) > limit:
        raise InvalidEvent(f"{key}: longer than {limit} characters")
    return text or None


def _flag(raw: Mapping[str, Any], key: str, default: bool) -> bool:
    value = raw.get(key)
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float) and value in (0, 1):
        return bool(value)
    if isinstance(value, str) and value.strip().lower() in _TRUE | _FALSE:
        return value.strip().lower() in _TRUE
    raise InvalidEvent(f"{key}: expected true or false, got {value!r}")


def _when(raw: Mapping[str, Any], key: str) -> datetime | None:
    value = raw.get(key)
    if isinstance(value, bool):
        raise InvalidEvent(f"{key}: expected a date, got {value!r}")
    try:
        return parse_dt(value)
    except (TypeError, ValueError, OverflowError, OSError) as exc:
        raise InvalidEvent(f"{key}: not an ISO-8601 date or Unix timestamp: {value!r}") from exc


def clip_summary(summary: str) -> str:
    """One line, whitespace collapsed, at most :data:`~argus_scout.events.SUMMARY_MAX` characters."""
    text = " ".join(summary.split())
    return text if len(text) <= SUMMARY_MAX else text[: SUMMARY_MAX - 1].rstrip() + "…"


def parse_envelope(
    raw: Any,
    *,
    default_source: str | None = None,
    default_event_id: str | None = None,
    now: datetime | None = None,
) -> StatusEvent:
    """Validate one envelope (a mapping) into a :class:`StatusEvent`, or raise :class:`InvalidEvent`.

    ``source`` and ``summary`` (or ``title``) are required unless defaults are given. A missing
    ``event_id`` falls back to ``default_event_id``, else a fresh random id: a producer that re-posts
    without an id is saying something new each time. Unknown keys are ignored.
    """
    if not isinstance(raw, Mapping):
        raise InvalidEvent(f"expected an object, got {type(raw).__name__}")
    source = _text(raw, "source", limit=ID_MAX) or default_source
    if not source:
        raise InvalidEvent("source: required")
    summary = _text(raw, "summary") or _text(raw, "title")
    if not summary:
        raise InvalidEvent("summary: required")
    event_id = _text(raw, "event_id", limit=ID_MAX) or default_event_id or f"{source}:{uuid4().hex[:12]}"
    payload = raw.get("payload")
    if payload is not None and not isinstance(payload, Mapping):
        raise InvalidEvent(f"payload: expected an object, got {type(payload).__name__}")
    schema = raw.get("schema_version")
    if schema is not None and (isinstance(schema, bool) or not isinstance(schema, int) or schema < 1):
        raise InvalidEvent(f"schema_version: expected a positive integer, got {schema!r}")
    severity = raw.get("severity")
    if severity is not None and not isinstance(severity, str | Severity):
        raise InvalidEvent(f"severity: expected text, got {type(severity).__name__}")
    return StatusEvent(
        event_id=event_id,
        source=source,
        severity=coerce_severity(severity),
        generated_at=_when(raw, "generated_at") or now or datetime.now(UTC),
        summary=clip_summary(summary),
        body_md=_text(raw, "body_md") or "",
        speakable=_flag(raw, "speakable", True),
        requires_ack=_flag(raw, "requires_ack", False),
        quiet_exempt=_flag(raw, "quiet_exempt", False),
        expires_at=_when(raw, "expires_at"),
        link=_text(raw, "link"),
        schema_version=schema or SCHEMA_VERSION,
        payload=dict(payload or {}),
    )


def _aware(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


def normalise(event: StatusEvent, *, now: datetime) -> StatusEvent:
    """A copy of ``event`` fit for the inbox: trimmed ids, clipped summary, coerced severity, aware
    datetimes, and none of the inbox's own bookkeeping (ack, announce state), which only scout sets."""
    source = str(event.source or "").strip()
    event_id = str(event.event_id or "").strip()
    summary = clip_summary(str(event.summary or ""))
    if not source:
        raise InvalidEvent("source: required")
    if not event_id:
        raise InvalidEvent("event_id: required")
    if not summary:
        raise InvalidEvent("summary: required")
    if len(source) > ID_MAX or len(event_id) > ID_MAX:
        raise InvalidEvent(f"source and event_id must be at most {ID_MAX} characters")
    return replace(
        event,
        source=source,
        event_id=event_id,
        summary=summary,
        severity=coerce_severity(event.severity),
        generated_at=_aware(event.generated_at) or now,
        expires_at=_aware(event.expires_at),
        acked_at=None,
        last_broadcast_at=None,
        broadcast_count=0,
        ingested_at=now,
    )


# --- the inbox -----------------------------------------------------------------------------------------------


class Inbox:
    """Inbox logic over a store. Thread-safe: ingests through one inbox are serialised."""

    def __init__(self, store: Store, *, clock: Callable[[], datetime] | None = None) -> None:
        self.store = store
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lock = threading.Lock()

    def now(self) -> datetime:
        return _aware(self._clock())  # type: ignore[return-value]

    def ingest(self, event: StatusEvent | Mapping[str, Any]) -> IngestResult:
        """Validate, normalise and upsert one event; raise :class:`InvalidEvent` if it is malformed."""
        now = self.now()
        if not isinstance(event, StatusEvent):
            event = parse_envelope(event, now=now)
        event = normalise(event, now=now)
        with self._lock:
            before = self.store.get_entry(event.source, event.event_id)
            duplicates: tuple[tuple[str, str], ...] = ()
            if before is None:
                duplicates = tuple(
                    (e.source, e.event_id)
                    for e in self.store.events_by_fingerprint(event.fingerprint)
                    if not e.expired(now)
                )
            self.store.upsert_event(event, now=now)
        if before is None:
            result = IngestResult(Outcome.DUPLICATE if duplicates else Outcome.NEW, event, duplicate_of=duplicates)
        else:
            previous = before.event.severity
            if before.resolved_at is not None:
                outcome = Outcome.RECURRED
            elif before.fingerprint == event.fingerprint:
                outcome = Outcome.REFRESHED
            elif rank(event.severity) > rank(previous):
                outcome = Outcome.ESCALATED
            else:
                outcome = Outcome.UPDATED
            reopened = outcome is not Outcome.REFRESHED and before.event.acked
            result = IngestResult(outcome, event, previous_severity=previous, reopened=reopened)
        log.info(
            "inbox.ingest",
            outcome=result.outcome.value,
            source=event.source,
            event_id=event.event_id,
            severity=event.severity.value,
            reopened=result.reopened,
        )
        return result

    def ack(self, source: str, event_id: str, by: str) -> bool:
        """Acknowledge an open event; False if it is unknown or already acked."""
        by = str(by or "").strip()
        if not by:
            raise ValueError("an ack needs to say who made it")
        acked = self.store.ack_event(source, event_id, by, at=self.now())
        log.info("inbox.ack", source=source, event_id=event_id, by=by, acked=acked)
        return acked

    def resolve_missing(self, source: str, present: Iterable[str]) -> list[str]:
        """Resolve ``source``'s events whose ids are not in ``present``: the source's latest full report no longer
        lists them, so the condition cleared. A system-side close, never an ack; seeing one again reopens it
        (:attr:`Outcome.RECURRED`). Returns the ids resolved. Call it only with a *complete, successful* view."""
        with self._lock:
            resolved = self.store.resolve_missing(source, present, at=self.now())
        if resolved:
            log.info("inbox.resolved", source=source, count=len(resolved), event_ids=resolved[:20])
        return resolved

    def open(self) -> list[StatusEvent]:
        """Unacked, unresolved, unexpired events, oldest first."""
        return self.store.open_events(now=self.now())

    def open_grouped(self) -> list[EventGroup]:
        """Open events collapsed by fingerprint (see :func:`~argus_scout.events.group_duplicates`),
        in first-seen order. The fingerprint includes the source, so a group is one source saying
        the same thing under several event ids."""
        return [EventGroup(group[0].fingerprint, group) for group in group_duplicates(self.open())]


__all__ = [
    "EventGroup",
    "Inbox",
    "IngestResult",
    "InvalidEvent",
    "Outcome",
    "clip_summary",
    "normalise",
    "parse_envelope",
]
