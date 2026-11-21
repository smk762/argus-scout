"""The jobs the scheduler runs: the status cycle, the analysis cycle and the briefings.

These are the seams later phases fill in. The structure is already the real one:

- :class:`StatusCycle` reloads the heartbeat policy through :class:`~argus_scout.heartbeat.PolicyFile` on
  every run, so a policy edit applies on the next tick and **a bad edit degrades to defaults while the
  cycle keeps running**. It reads open events from :meth:`Inbox.open_grouped <argus_scout.inbox.Inbox.open_grouped>`,
  skips rest hours, and decides each group through :func:`~argus_scout.heartbeat.explain`. Composing and
  delivering the update comes later.
- :class:`PollJob` polls one polling source and ingests what it returns through
  :meth:`Inbox.ingest <argus_scout.inbox.Inbox.ingest>`. Each source is its own job, so one failing source
  never affects another.
- :class:`AnalysisCycle` will evaluate signal rules against Prometheus and raise recommendations (phase 2).
- :class:`BriefingJob` will generate a scheduled report (phase 3); for now it only records that it ran.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

import structlog

from argus_scout.config import ScoutConfig
from argus_scout.heartbeat import Decision, HeartbeatPolicy, PolicyFile, explain
from argus_scout.inbox import EventGroup, Inbox, InvalidEvent
from argus_scout.scheduler import Clock, DailyAt, Every, Scheduler, utc_now
from argus_scout.sources import BoundSource, Sources, build_sources
from argus_scout.store.base import Store

log = structlog.get_logger()

DEFAULT_POLL_INTERVAL = 60.0


@dataclass
class StatusResult:
    """What one status cycle saw and decided."""

    at: datetime
    policy: HeartbeatPolicy
    resting: bool = False
    quiet: bool = False
    groups: list[EventGroup] = field(default_factory=list)
    decisions: list[Decision] = field(default_factory=list)
    """One per group, decided on the group's latest event."""

    @property
    def reportable(self) -> list[Decision]:
        return [d for d in self.decisions if d.report]


class StatusCycle:
    """One status tick: load the policy, read the inbox, decide what is worth reporting."""

    def __init__(
        self,
        store: Store,
        policy_file: PolicyFile,
        *,
        inbox: Inbox | None = None,
        tz: ZoneInfo | None = None,
        clock: Clock = utc_now,
    ) -> None:
        self.store = store
        self.policy_file = policy_file
        self.inbox = inbox or Inbox(store, clock=clock)
        self.tz = tz or ZoneInfo("UTC")
        self.clock = clock
        self.last: StatusResult | None = None

    def load_policy(self) -> HeartbeatPolicy:
        """The current policy. Never raises: anything wrong with the file means the defaults."""
        try:
            return self.policy_file.get()
        except Exception as exc:  # noqa: BLE001 — PolicyFile already falls back; this is the last line
            log.warning("status.policy_unavailable", error=str(exc))
            return HeartbeatPolicy(path=self.policy_file.path, file_error=str(exc))

    async def __call__(self) -> StatusResult:
        now = self.clock()
        local = now.astimezone(self.tz)
        policy = self.load_policy()
        result = StatusResult(at=now, policy=policy)
        if policy.file_error:
            log.warning("status.policy_defaults", path=policy.path, error=policy.file_error)
        if policy.rest_hours.contains(local):
            result.resting = True
            log.info("status.resting", until=policy.rest_hours.end)
        else:
            result.quiet = policy.quiet_hours.contains(local)
            result.groups = self.inbox.open_grouped()
            result.decisions = [explain(g.latest, policy, now=now, quiet=result.quiet) for g in result.groups]
            log.info("status.cycle", groups=len(result.groups), reportable=len(result.reportable), quiet=result.quiet)
            # Composing and delivering the update arrives with the narrative and channels phases.
        self.last = result
        return result


class AnalysisCycle:
    """One analysis tick. Signal rules against Prometheus land here in phase 2."""

    def __init__(self, store: Store, *, clock: Clock = utc_now) -> None:
        self.store = store
        self.clock = clock
        self.last_run: datetime | None = None

    async def __call__(self) -> None:
        self.last_run = self.clock()
        log.info("analysis.cycle", signals=0)


class BriefingJob:
    """A scheduled briefing of one ``kind``. Report generation lands in phase 3; for now it records the run."""

    def __init__(self, kind: str, store: Store, *, clock: Clock = utc_now) -> None:
        self.kind = kind
        self.store = store
        self.clock = clock
        self.runs: list[datetime] = []

    async def __call__(self) -> None:
        at = self.clock()
        self.runs.append(at)
        log.info("briefing.due", kind=self.kind, at=at.isoformat())


class PollJob:
    """Poll one source and ingest what it returns. A failed poll raises, so the scheduler counts and retries
    it; an event the inbox refuses is logged and skipped, and the rest of the batch still lands."""

    def __init__(self, source: BoundSource, inbox: Inbox) -> None:
        self.source = source
        self.inbox = inbox
        self.outcomes: Counter[str] = Counter()

    @property
    def interval(self) -> float:
        return float(getattr(self.source.config, "interval", None) or DEFAULT_POLL_INTERVAL)

    async def __call__(self) -> None:
        events = await self.source.plugin.poll(self.source.config)  # type: ignore[attr-defined]
        batch: Counter[str] = Counter()
        for event in events:
            try:
                batch[self.inbox.ingest(event).outcome.value] += 1
            except InvalidEvent as exc:
                batch["refused"] += 1
                log.warning("poll.refused", source=self.source.name, error=str(exc))
        self.outcomes.update(batch)
        log.info("poll.done", source=self.source.name, events=len(events), **dict(batch))


def register_jobs(
    scheduler: Scheduler,
    config: ScoutConfig,
    store: Store,
    policy_file: PolicyFile,
    *,
    inbox: Inbox | None = None,
    sources: Sources | None = None,
) -> None:
    """Register the status cycle, the analysis cycle, one job per briefing and one per polling source.

    Job names are ``status``, ``analysis``, ``briefing:<kind>`` (``briefing:<kind>@HH:MM`` when a kind is
    scheduled more than once) and ``poll:<source>``. The name keys the persisted bookkeeping, so keep it
    stable. A source whose config doesn't bind gets no job (``build_sources`` logs why); the rest still do.
    """
    schedule = config.schedule
    clock = scheduler.clock
    inbox = inbox or Inbox(store, clock=clock)
    sources = sources if sources is not None else build_sources(config)
    status = StatusCycle(store, policy_file, inbox=inbox, tz=schedule.tz, clock=clock)
    scheduler.add("status", status, Every(schedule.status_interval))
    scheduler.add("analysis", AnalysisCycle(store, clock=clock), Every(schedule.analysis_interval))
    kinds = Counter(b.kind for b in schedule.briefings)
    for briefing in schedule.briefings:
        name = f"briefing:{briefing.kind}" + (f"@{briefing.at}" if kinds[briefing.kind] > 1 else "")
        job = BriefingJob(briefing.kind, store, clock=clock)
        scheduler.add(name, job, DailyAt(briefing.at_time, schedule.tz))
    for source in sources.polling:
        poll = PollJob(source, inbox)
        scheduler.add(f"poll:{source.name}", poll, Every(poll.interval), retry_delay=min(60.0, poll.interval))


__all__ = ["AnalysisCycle", "BriefingJob", "PollJob", "StatusCycle", "StatusResult", "register_jobs"]
