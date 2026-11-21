"""Daily usage caps: a hard ceiling on LLM generations, speech syntheses and spoken deliveries.

Shared GPUs and the house speakers need a ceiling in case a scheduler loop or a
retry bug misbehaves.

- Each kind (``llm``, ``tts``, ``speak``, or anything else) has a daily limit;
  ``0`` or no entry means unlimited.
- Counters reset at local midnight. With a ``state_path`` they persist, so a
  crash loop cannot reset them.
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
from typing import Any

import structlog

log = structlog.get_logger()

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


class DailyBudget:
    def __init__(
        self,
        limits: Mapping[str, int] | None = None,
        *,
        state_path: str | Path | None = None,
        block_manual: bool = False,
        today: Callable[[], date] = local_today,
        on_change: Callable[[dict[str, int], dict[str, int]], None] | None = None,
    ) -> None:
        """``on_change(counts, limits)`` runs after every change, e.g. to update gauges."""
        self.limits = {k: max(0, int(v or 0)) for k, v in (limits or {}).items()}
        self.state_path = Path(state_path) if state_path else None
        self.block_manual = block_manual
        self._today = today
        self._on_change = on_change
        self._lock = threading.Lock()
        self._day: date | None = None
        self._counts: dict[str, int] = {}
        self._load()

    def limit_for(self, kind: str) -> int:
        return self.limits.get(kind, 0)

    # -- persistence -------------------------------------------------------
    def _load(self) -> None:
        if not self.state_path or not self.state_path.exists():
            return
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            self._day = date.fromisoformat(str(data.get("day")))
            self._counts = {str(k): max(0, int(v or 0)) for k, v in (data.get("counts") or {}).items()}
        except Exception as exc:  # noqa: BLE001 — start from zero rather than refuse to run
            log.warning("budget.load_failed", error=str(exc), path=str(self.state_path))

    def _save(self) -> None:
        if not self.state_path:
            return
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
            payload = {"day": self._day.isoformat() if self._day else None, "counts": self._counts}
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            tmp.replace(self.state_path)
        except Exception as exc:  # noqa: BLE001
            log.warning("budget.save_failed", error=str(exc), path=str(self.state_path))

    def _roll(self) -> None:
        today = self._today()
        if self._day != today:
            if self._day is not None and any(self._counts.values()):
                log.info("budget.reset", previous_day=self._day.isoformat(), counts=self._counts)
            self._day = today
            self._counts = {}
            self._save()
            self._publish()

    def _publish(self) -> None:
        if self._on_change:
            self._on_change(dict(self._counts), dict(self.limits))

    # -- queries -----------------------------------------------------------
    def used(self, kind: str) -> int:
        with self._lock:
            self._roll()
            return self._counts.get(kind, 0)

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
        """Record one unit of ``kind`` if allowed. Returns False when refused."""
        manual = is_manual()
        limit = self.limit_for(kind)
        with self._lock:
            self._roll()
            used = self._counts.get(kind, 0)
            over = limit > 0 and used >= limit
            if over and (not manual or self.block_manual):
                log.warning("budget.exhausted", kind=kind, used=used, limit=limit, manual=manual)
                return False
            self._counts[kind] = used + 1
            if over:
                log.warning("budget.manual_over_limit", kind=kind, used=used + 1, limit=limit)
            self._save()
            self._publish()
            return True

    def consume(self, kind: str) -> None:
        """Like :meth:`try_consume` but raises :class:`BudgetExhaustedError` when refused."""
        if not self.try_consume(kind):
            raise BudgetExhaustedError(kind, self.limit_for(kind))

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            self._roll()
            day = self._day.isoformat() if self._day else None
            counts = dict(self._counts)
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
