"""The redelivery sweep: try again, later, the reports a channel failed to take.

A channel that fails is recorded in the store's ``deliveries`` table (the webhook channel's own retries all
count as one attempt). The ``redeliver`` scheduler job runs every ``delivery.retry_interval`` seconds and
sends each such report again, **to the failed channels only**, through the same
:class:`~argus_scout.channels.dispatcher.Dispatcher`, so the usual rules hold:

- **Idempotent.** The dispatcher skips a channel that already has the message, and the store keeps at most one
  successful row per (message, channel), so a retry that races another delivery still sends once.
- **Bounded.** A channel is retried while it has fewer than ``delivery.max_attempts`` failed attempts at the
  report and the report is younger than ``delivery.retry_max_age``. A failure the channel refused
  **permanently** (a 4xx other than 408 or 429, or a synthesis request echo refused) is never retried.
- **No late speech.** A report older than ``delivery.speak_max_age`` (default ten minutes) is retried on text
  channels only: a briefing spoken an hour late is worse than none. Voice still passes the heartbeat gates, so
  a retry during quiet hours is held back like any other delivery.
- **Only what failed.** A channel the report was never tried on (filtered, held back by the voice gate, added to
  the config later, or one that no longer exists) is left alone: the sweep repairs failures, it doesn't backfill.
- **Skips aren't failures.** A voice channel that answers ``skipped`` (an argus-mask face with no ready screen, or
  a cloned voice it refuses) writes no row, so a skip never counts toward ``max_attempts``, and a channel that only
  ever skipped is never retried. One that failed first keeps being retried under the same limits, ``speak_max_age``
  included, whatever it answers in between.

Reports only: an inbox item is re-announced by the status cycle on its own schedule.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

import structlog

from argus_scout.channels.base import Delivery
from argus_scout.channels.dispatcher import Dispatcher, DispatchReport
from argus_scout.config import DeliveryConfig
from argus_scout.scheduler import Clock, utc_now
from argus_scout.store.base import Report, Store

log = structlog.get_logger(__name__)

JOB_NAME = "redeliver"


@dataclass
class SweepResult:
    """What one sweep tried and what came of it, as ``(delivery key, channel)`` pairs."""

    at: datetime
    reports: list[DispatchReport] = field(default_factory=list)
    stale_voice: list[tuple[str, str]] = field(default_factory=list)
    """Voice channels skipped because the report was too old to speak."""

    @property
    def sent(self) -> list[tuple[str, str]]:
        return [(r.key, c) for r in self.reports for c in r.sent]

    @property
    def failed(self) -> list[tuple[str, str]]:
        return [(r.key, c) for r in self.reports for c in r.failed]


class RedeliveryJob:
    """One sweep per call. See the module docs for the policy."""

    def __init__(
        self, store: Store, dispatcher: Dispatcher, config: DeliveryConfig | None = None, *, clock: Clock = utc_now
    ) -> None:
        self.store = store
        self.dispatcher = dispatcher
        self.config = config or DeliveryConfig()
        self.clock = clock
        self.last: SweepResult | None = None

    def may_speak(self, report: Report, now: datetime) -> bool:
        """Whether a retry of ``report`` may still be spoken: younger than ``speak_max_age``."""
        return (now - report.generated_at).total_seconds() < self.config.speak_max_age

    async def __call__(self) -> SweepResult:
        now = self.clock()
        result = SweepResult(at=now)
        since = now - timedelta(seconds=self.config.retry_max_age)
        pending = self.store.undelivered_reports(since, max_failures=self.config.max_attempts)
        configured = {b.name: b for b in self.dispatcher.channels}
        by_report: dict[int, tuple[Report, list[str]]] = {}
        for item in pending:
            if item.channel in configured:
                by_report.setdefault(item.report.id, (item.report, []))[1].append(item.channel)
        for report, names in by_report.values():
            message = Delivery.from_report(report)
            speak = self.may_speak(report, now)
            stale = [n for n in names if configured[n].voice and not speak]
            result.stale_voice += [(message.key, n) for n in stale]
            only = [n for n in names if n not in stale]
            if stale:
                log.info("redeliver.too_late_to_speak", key=message.key, channels=stale)
            if not only:
                continue
            log.info("redeliver.retry", key=message.key, channels=only)
            result.reports.append(await self.dispatcher.deliver(message, only=only))
        if result.reports or result.stale_voice:
            log.info("redeliver.done", sent=len(result.sent), failed=len(result.failed), stale=len(result.stale_voice))
        self.last = result
        return result


__all__ = ["JOB_NAME", "RedeliveryJob", "SweepResult"]
