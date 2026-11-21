"""The storage contract: what scout persists, independent of the backend.

Every backend stores the existing domain types (:class:`StatusEvent`,
:class:`Recommendation`, :class:`StandingRule`); the only types defined here
are the records the domain types don't already carry (inbox bookkeeping, the
transition audit trail, reports).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from argus_scout.approvals import StandingRule
from argus_scout.events import StatusEvent
from argus_scout.recommendations import Recommendation, Status


@dataclass
class InboxEntry:
    """A stored status event plus the inbox's own bookkeeping."""

    event: StatusEvent
    fingerprint: str
    first_seen: datetime
    last_seen: datetime
    seen_count: int = 1
    acked_by: str | None = None


@dataclass
class Transition:
    """One recorded status change of a recommendation (the audit trail)."""

    rec_id: str
    from_status: Status | None
    to_status: Status
    at: datetime
    by: str | None = None
    source: str | None = None  # "manual" | "standing" for approvals
    rule_id: str | None = None
    reason: str = ""


@dataclass
class Report:
    """A generated briefing or status report."""

    id: int
    kind: str
    generated_at: datetime
    text: str
    meta: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class Store(Protocol):
    """A persistence backend. :class:`~argus_scout.store.sqlite.SqliteStore` is the default."""

    def close(self) -> None: ...

    # -- status events (the inbox) ------------------------------------------
    def upsert_event(self, event: StatusEvent, *, now: datetime | None = None) -> bool:
        """Insert or refresh by ``(source, event_id)``. Returns True when it was new."""
        ...

    def get_event(self, source: str, event_id: str) -> StatusEvent | None: ...

    def get_entry(self, source: str, event_id: str) -> InboxEntry | None: ...

    def ack_event(self, source: str, event_id: str, by: str, *, at: datetime | None = None) -> bool: ...

    def mark_announced(self, source: str, event_id: str, *, at: datetime | None = None) -> bool: ...

    def open_events(self, *, now: datetime | None = None, include_expired: bool = False) -> list[StatusEvent]: ...

    def events_by_fingerprint(self, fingerprint: str, *, open_only: bool = True) -> list[StatusEvent]: ...

    def open_fingerprints(self) -> dict[str, int]: ...

    # -- recommendations -----------------------------------------------------
    def save_recommendation(self, rec: Recommendation, *, by: str | None = None, reason: str | None = None) -> None:
        """Insert or update; a status change appends a :class:`Transition`."""
        ...

    def get_recommendation(self, rec_id: str) -> Recommendation | None: ...

    def list_recommendations(
        self, status: Status | Iterable[Status] | None = None, *, limit: int | None = None
    ) -> list[Recommendation]: ...

    def recommendation_history(self, rec_id: str) -> list[Transition]: ...

    # -- standing approvals (runtime rules only) ------------------------------
    def load_runtime_rules(self) -> list[StandingRule]: ...

    def save_runtime_rule(self, rule: StandingRule) -> None: ...

    def delete_runtime_rule(self, rule_id: str) -> bool: ...

    # -- reports ---------------------------------------------------------------
    def add_report(
        self, kind: str, generated_at: datetime, text: str, meta: dict[str, Any] | None = None
    ) -> Report: ...

    def latest_report(self, kind: str | None = None) -> Report | None: ...

    def list_reports(self, limit: int = 50, *, kind: str | None = None) -> list[Report]: ...

    # -- retention ---------------------------------------------------------------
    def prune(self, older_than: datetime) -> dict[str, int]: ...


__all__ = ["InboxEntry", "Report", "Store", "Transition"]
