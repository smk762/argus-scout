"""The storage contract: what scout persists, independent of the backend.

Every backend stores the existing domain types (:class:`StatusEvent`,
:class:`Recommendation`, :class:`StandingRule`); the only types defined here
are the records the domain types don't already carry (inbox bookkeeping, the
transition audit trail, reports).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from argus_scout.approvals import StandingRule
from argus_scout.events import StatusEvent
from argus_scout.recommendations import Recommendation, Status


class Unchecked:
    """The type of :data:`UNCHECKED`."""

    def __repr__(self) -> str:
        return "UNCHECKED"


UNCHECKED = Unchecked()
"""``expected_status`` default: the caller makes no claim about what is stored (seeding, imports, tests)."""


class StoreConflict(Exception):
    """A recommendation write was refused and nothing was written. Reload and decide again."""


class IllegalTransition(StoreConflict):
    """The write would move a stored recommendation along a step the lifecycle doesn't allow."""


class StaleRecommendation(StoreConflict):
    """The stored status is no longer the one the caller loaded: someone else changed it in between."""


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


@dataclass
class JobRun:
    """The scheduler's bookkeeping for one job, so its schedule and counters survive a restart."""

    name: str
    last_run_at: datetime | None = None  # start of the last successful run: what the schedule keys on
    last_success_at: datetime | None = None
    last_error_at: datetime | None = None
    last_error: str | None = None
    run_count: int = 0
    error_count: int = 0


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
    def save_recommendation(
        self,
        rec: Recommendation,
        *,
        by: str | None = None,
        reason: str | None = None,
        expected_status: Status | None | Unchecked = UNCHECKED,
    ) -> None:
        """Insert or update; a status change appends a :class:`Transition`.

        Atomically, before writing: if ``expected_status`` is given (``None`` meaning "no row yet") and the
        stored status differs, raise :class:`StaleRecommendation`; if a stored row's status would change to one
        ``TRANSITIONS`` doesn't allow from it, raise :class:`IllegalTransition`. Either way nothing is written.
        """
        ...

    def get_recommendation(self, rec_id: str) -> Recommendation | None: ...

    def list_recommendations(
        self, status: Status | Iterable[Status] | None = None, *, limit: int | None = None
    ) -> list[Recommendation]: ...

    def recommendations_by_key(self, key: str, status: Status | Iterable[Status] | None = None) -> list[Recommendation]:
        """Recommendations with this dedup key, most recently updated first."""
        ...

    def recent_rejections(self, limit: int = 10) -> list[Recommendation]:
        """Rejected recommendations, most recently rejected first."""
        ...

    def recommendation_history(self, rec_id: str) -> list[Transition]: ...

    # -- signal rules' pending ``for`` state ---------------------------------
    def load_signal_pending(self) -> dict[str, dict[str, datetime]]: ...

    def save_signal_pending(self, rule: str, pending: dict[str, datetime]) -> None: ...

    def drop_signal_pending(self, keep: Iterable[str]) -> int: ...

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

    # -- scheduler bookkeeping ------------------------------------------------------
    def get_job_run(self, name: str) -> JobRun | None: ...

    def list_job_runs(self) -> list[JobRun]: ...

    def record_job_run(
        self, name: str, *, started_at: datetime, finished_at: datetime, error: str | None = None
    ) -> JobRun: ...

    # -- daily usage counts (:class:`~argus_scout.budget.UsageCounter`) ------------------------
    def usage_counts(self, day: str) -> dict[str, int]: ...

    def consume_usage(self, day: str, kind: str, limit: int, *, allow_over: bool = False) -> tuple[bool, int]:
        """Atomically charge one ``kind`` on ``day`` unless it is at ``limit`` (0: unlimited); safe across
        processes sharing the store. Returns (charged, count after)."""
        ...

    def merge_usage(self, day: str, counts: Mapping[str, int]) -> None:
        """Raise each count to at least the given one (importing an old ``usage.json``)."""
        ...

    # -- counts for /metrics ------------------------------------------------------------
    def recommendation_counts(self) -> dict[Status, int]: ...

    def approval_counts(self) -> dict[str, int]: ...

    def open_event_counts(self, *, now: datetime | None = None) -> dict[str, int]: ...

    # -- retention ---------------------------------------------------------------
    def prune(self, older_than: datetime) -> dict[str, int]: ...


__all__ = [
    "UNCHECKED",
    "IllegalTransition",
    "InboxEntry",
    "JobRun",
    "Report",
    "StaleRecommendation",
    "Store",
    "StoreConflict",
    "Transition",
    "Unchecked",
]
