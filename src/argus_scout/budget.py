"""Daily usage caps: a hard ceiling on LLM generations, speech syntheses and spoken deliveries.

Shared GPUs and the house speakers need a ceiling in case a scheduler loop or a
retry bug misbehaves.

- Each kind (``llm``, ``tts``, ``speak``, or anything else) has a daily limit;
  ``0`` or no entry means unlimited.
- Counters reset at local midnight. They persist, so a crash loop cannot reset
  them: in the store (a :class:`UsageCounter`), where each charge is one atomic
  check-and-increment, so the server and a CLI run sharing the database can
  neither lose a count nor together exceed a cap; or, for a single process, in a
  ``state_path`` JSON file.
- Background work (the scheduler) is refused once a limit is hit. Work done
  while serving an HTTP request (see :class:`ManualOriginMiddleware`) still goes
  through and is logged, unless ``block_manual`` is set.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Mapping
from contextvars import ContextVar, Token
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

import structlog

if TYPE_CHECKING:
    from argus_scout.config import ScoutConfig

log = structlog.get_logger()

USAGE_STATE_FILE = "usage.json"
"""Where older versions kept the daily counts, beside the store. Read once and imported into the store."""

# True while handling an HTTP request. Scheduler jobs run outside any request.
_manual_origin: ContextVar[bool] = ContextVar("scout_budget_manual_origin", default=False)


def is_manual() -> bool:
    return _manual_origin.get()


def set_manual(value: bool = True) -> Token[bool]:
    """Mark the current context as manual; returns a token for :func:`reset_manual`."""
    return _manual_origin.set(value)


def reset_manual(token: Token[bool]) -> None:
    _manual_origin.reset(token)


def local_today() -> date:
    return datetime.now().astimezone().date()


class BudgetExhaustedError(RuntimeError):
    """Raised when a call would exceed today's limit."""

    def __init__(self, kind: str, limit: int) -> None:
        super().__init__(f"daily {kind} limit reached ({limit})")
        self.kind = kind
        self.limit = limit


class UsageCounter(Protocol):
    """Where a :class:`DailyBudget` keeps its counts so they hold across processes.

    :class:`~argus_scout.store.sqlite.SqliteStore` implements it: one row per (day, kind), charged by a single
    atomic upsert that checks the limit, so the server and a CLI run sharing a database can never lose a count
    or both take the last call under the cap.
    """

    def usage_counts(self, day: str) -> dict[str, int]:
        """Every kind's count for ``day`` (an ISO date)."""
        ...

    def consume_usage(self, day: str, kind: str, limit: int, *, allow_over: bool = False) -> tuple[bool, int]:
        """Add one to ``kind`` on ``day`` unless it is at ``limit`` (``0``: unlimited; ``allow_over``: charge
        anyway). Returns whether it was charged and the count after."""
        ...

    def merge_usage(self, day: str, counts: Mapping[str, int]) -> None:
        """Raise each kind's count on ``day`` to at least ``counts[kind]`` (never lowers one)."""
        ...


class DailyBudget:
    """Per-kind daily caps.

    The counts live in a ``counter`` (the store: safe across processes, how scout runs), in a ``state_path``
    JSON file (one process only, since two writers lose counts), or in memory (tests).
    """

    def __init__(
        self,
        limits: Mapping[str, int] | None = None,
        *,
        state_path: str | Path | None = None,
        counter: UsageCounter | None = None,
        block_manual: bool = False,
        today: Callable[[], date] = local_today,
        on_change: Callable[[dict[str, int], dict[str, int]], None] | None = None,
    ) -> None:
        """``on_change(counts, limits)`` runs after every change, e.g. to update gauges."""
        self.limits = {k: max(0, int(v or 0)) for k, v in (limits or {}).items()}
        self.state_path = Path(state_path) if state_path else None
        self.counter = counter
        self.block_manual = block_manual
        self._today = today
        self._on_change = on_change
        self._lock = threading.Lock()
        self._day: date | None = None
        self._counts: dict[str, int] = {}
        if counter is None and self.state_path is not None:
            loaded = read_usage_file(self.state_path)
            if loaded is not None:
                self._day, self._counts = loaded

    def limit_for(self, kind: str) -> int:
        return self.limits.get(kind, 0)

    # -- persistence -------------------------------------------------------
    def _save(self) -> None:
        if not self.state_path or self.counter is not None:
            return
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
            payload = {"day": self._day.isoformat() if self._day else None, "counts": self._counts}
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            tmp.replace(self.state_path)
        except Exception as exc:  # noqa: BLE001
            log.warning("budget.save_failed", error=str(exc), path=str(self.state_path))

    def _roll(self) -> str:
        """Start a new day at local midnight; returns today's ISO date. With a counter the day is part of each
        row's key, so only this process's cache (and the log line) resets."""
        today = self._today()
        if self._day != today:
            if self._day is not None and any(self._counts.values()):
                log.info("budget.reset", previous_day=self._day.isoformat(), counts=self._counts)
            self._day = today
            self._counts = {}
            self._save()
            self._publish()
        return today.isoformat()

    def _current(self, day: str) -> dict[str, int]:
        """Today's counts, read from the counter when there is one (call with the lock held)."""
        if self.counter is not None:
            self._counts = self.counter.usage_counts(day)
        return dict(self._counts)

    def _publish(self) -> None:
        if self._on_change:
            self._on_change(dict(self._counts), dict(self.limits))

    # -- queries -----------------------------------------------------------
    def used(self, kind: str) -> int:
        with self._lock:
            return self._current(self._roll()).get(kind, 0)

    def exhausted(self, kind: str) -> bool:
        limit = self.limit_for(kind)
        return limit > 0 and self.used(kind) >= limit

    def available(self, kind: str) -> bool:
        """Whether a call of ``kind`` would be allowed from the current context."""
        if not self.exhausted(kind):
            return True
        return is_manual() and not self.block_manual

    # -- mutation ----------------------------------------------------------
    def try_consume(self, kind: str) -> bool:
        """Record one unit of ``kind`` if allowed. Returns False when refused.

        With a counter, the limit check and the increment are one transaction in the store, so concurrent
        processes can't both take the last unit."""
        manual = is_manual()
        allow_over = manual and not self.block_manual
        limit = self.limit_for(kind)
        with self._lock:
            day = self._roll()
            if self.counter is not None:
                ok, used = self.counter.consume_usage(day, kind, limit, allow_over=allow_over)
                self._counts[kind] = used
            else:
                used = self._counts.get(kind, 0)
                ok = limit <= 0 or used < limit or allow_over
                if ok:
                    used += 1
                    self._counts[kind] = used
            if not ok:
                log.warning("budget.exhausted", kind=kind, used=used, limit=limit, manual=manual)
                return False
            if 0 < limit < used:
                log.warning("budget.manual_over_limit", kind=kind, used=used, limit=limit)
            self._save()
            self._publish()
            return True

    def consume(self, kind: str) -> None:
        """Like :meth:`try_consume` but raises :class:`BudgetExhaustedError` when refused."""
        if not self.try_consume(kind):
            raise BudgetExhaustedError(kind, self.limit_for(kind))

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            day = self._roll()
            counts = self._current(day)
        kinds = sorted(set(counts) | set(self.limits))
        return {
            "day": day,
            "block_manual": self.block_manual,
            "kinds": {
                kind: {
                    "used": counts.get(kind, 0),
                    "limit": self.limit_for(kind),
                    "exhausted": 0 < self.limit_for(kind) <= counts.get(kind, 0),
                }
                for kind in kinds
            },
        }


def read_usage_file(path: Path) -> tuple[date, dict[str, int]] | None:
    """The day and counts in a ``usage.json``; ``None`` when it is missing or unreadable (logged)."""
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        day = date.fromisoformat(str(data.get("day")))
        return day, {str(k): max(0, int(v or 0)) for k, v in (data.get("counts") or {}).items()}
    except Exception as exc:  # noqa: BLE001 — start from zero rather than refuse to run
        log.warning("budget.load_failed", error=str(exc), path=str(path))
        return None


def import_usage_file(path: Path, counter: UsageCounter, *, rename: bool = True) -> bool:
    """Fold a ``usage.json`` left by an older scout into ``counter``, then rename it ``usage.json.migrated``.

    Counts merge by taking the larger of the two, so two processes importing at once, or an import retried after
    a crash before the rename, never double a count. ``rename=False`` (a dry run) leaves the file where it is.
    Returns True when a file was imported."""
    loaded = read_usage_file(path)
    if loaded is None:
        return False
    day, counts = loaded
    counter.merge_usage(day.isoformat(), counts)
    if not rename:
        return True
    try:
        path.replace(path.with_name(path.name + ".migrated"))
    except FileNotFoundError:
        pass  # another process imported and renamed it first
    except OSError as exc:
        log.warning("budget.migrate_rename_failed", error=str(exc), path=str(path))
    log.info("budget.migrated", path=str(path), day=day.isoformat(), counts=counts)
    return True


def budget_from_config(
    config: ScoutConfig, *, counter: UsageCounter | None = None, persist: bool = True
) -> DailyBudget:
    """The budget ``budget:`` in scout.yaml describes.

    With ``counter`` (the store: how the server and the CLI call it) the counts live in the store and hold across
    processes; a :data:`USAGE_STATE_FILE` an older scout left beside the store is imported into it once. Without
    one they are kept in :data:`USAGE_STATE_FILE`, which only one process may write.

    ``persist=False`` reads today's counts but never writes them (a dry run: pass the dry run's in-memory copy of
    the store as ``counter``, and the old file is read into the copy but left in place)."""
    db = str(config.store.path)
    state = None if db == ":memory:" else Path(db).parent / USAGE_STATE_FILE
    limits, block = config.budget.limits, config.budget.block_manual
    if counter is not None:
        if state is not None:
            import_usage_file(state, counter, rename=persist)
        return DailyBudget(limits, counter=counter, block_manual=block)
    budget = DailyBudget(limits, state_path=state, block_manual=block)
    if not persist:
        budget.state_path = None
    return budget


class ManualOriginMiddleware:
    """Pure ASGI middleware: work done while serving an HTTP request counts as manual."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        token = set_manual(True)
        try:
            await self.app(scope, receive, send)
        finally:
            reset_manual(token)
