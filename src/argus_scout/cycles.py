"""The jobs the scheduler runs: the status cycle, the analysis cycle and the briefings.

These are the seams later phases fill in. The structure is already the real one:

- :class:`StatusCycle` reloads the heartbeat policy through :class:`~argus_scout.heartbeat.PolicyFile` on
  every run, so a policy edit applies on the next tick and **a bad edit degrades to defaults while the
  cycle keeps running**. It reads open events from :meth:`Inbox.open_grouped <argus_scout.inbox.Inbox.open_grouped>`,
  skips rest hours, and decides each group through :func:`~argus_scout.heartbeat.explain`. When groups pass
  :func:`~argus_scout.heartbeat.report_gate` (capped at ``report.max_items``, most severe first) and they
  differ from the last status report's, or the most severe one's ``reannounce_after`` has passed, it has
  the :class:`~argus_scout.narrative.Narrator` write a ``status`` report, stores it and hands it to
  :func:`deliver`.
- :class:`PollJob` polls one polling source and ingests what it returns through
  :meth:`Inbox.ingest <argus_scout.inbox.Inbox.ingest>`. Each source is its own job, so one failing source
  never affects another.
- :class:`AnalysisCycle` reloads the signal rules through :class:`~argus_scout.signals.SignalRulesFile`,
  evaluates them against Prometheus and hands the hits to the
  :class:`~argus_scout.engine.RecommendationEngine`. A bad rule is skipped; a file that won't parse skips
  the cycle (keeping the pending ``for`` clocks); a Prometheus outage makes rules *unknown*, which the
  engine never reads as cleared. Tariff-aware rules (:mod:`~argus_scout.tariff_rules`) in the same file are
  evaluated against the :class:`~argus_scout.tariff.TariffFile` and merged in; no tariff skips just them.
- :class:`BriefingJob` generates, stores and hands to :func:`deliver` one scheduled report (``morning``,
  ``evening`` or ``status``). With no model, or the model down or over its cap, the report comes from the
  template: a briefing is never skipped.

:func:`deliver` is the seam delivery channels (phase 5) plug into; for now it does nothing, so reports are
stored and logged only.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, tzinfo
from zoneinfo import ZoneInfo

import structlog

from argus_scout.approvals import StandingApprovals
from argus_scout.budget import DailyBudget
from argus_scout.channels.dispatcher import Dispatcher
from argus_scout.channels.redeliver import JOB_NAME as REDELIVER_JOB
from argus_scout.channels.redeliver import RedeliveryJob
from argus_scout.config import ScoutConfig
from argus_scout.engine import CycleReport, RecommendationEngine
from argus_scout.heartbeat import Decision, HeartbeatPolicy, PolicyFile, explain
from argus_scout.inbox import EventGroup, Inbox, InvalidEvent
from argus_scout.narrative import Narrator, build_narrator
from argus_scout.prometheus import PrometheusClient
from argus_scout.scheduler import Clock, DailyAt, Every, Scheduler, utc_now
from argus_scout.severity import rank
from argus_scout.signals import SignalRulesFile, SignalState, evaluate
from argus_scout.sources import BoundSource, Sources, build_sources
from argus_scout.store.base import Report, Store
from argus_scout.tariff import TariffFile
from argus_scout.tariff_rules import evaluate_tariff

log = structlog.get_logger()

DEFAULT_POLL_INTERVAL = 60.0

Deliver = Callable[[Report], Awaitable[None]]


async def deliver(report: Report) -> None:
    """Hand a stored report to the delivery channels. A no-op until channels land (phase 5): reports are
    stored and logged, and read from the store. ``BriefingJob`` and ``StatusCycle`` take a ``deliver``
    callable, so the channels dispatcher is injected there without touching the jobs."""


_NO_DELIVERY = deliver


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
    report: Report | None = None
    """The status report written this cycle, if one was due."""

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
        narrator: Narrator | None = None,
        deliver: Deliver | None = None,
    ) -> None:
        self.store = store
        self.policy_file = policy_file
        self.inbox = inbox or Inbox(store, clock=clock)
        self.tz = tz or ZoneInfo("UTC")
        self.clock = clock
        self.narrator = narrator
        self.deliver: Deliver = deliver or _NO_DELIVERY
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
            if self.narrator is not None:
                result.report = await self._report(result, policy, now)
        self.last = result
        return result

    def reportable_groups(self, result: StatusResult, policy: HeartbeatPolicy) -> list[EventGroup]:
        """The groups the report gate let through, most severe first, at most ``report.max_items``."""
        groups = [g for g, d in zip(result.groups, result.decisions, strict=True) if d.report]
        groups.sort(key=lambda g: (-rank(g.latest.severity), -g.latest.generated_at.timestamp()))
        return groups[: policy.report_max_items] if policy.report_max_items > 0 else groups

    def report_due(self, groups: list[EventGroup], policy: HeartbeatPolicy, now: datetime) -> bool:
        """Whether these groups are news: none reported yet, different from the last status report's, or the
        most severe one's ``reannounce_after`` interval has passed since it."""
        if not groups:
            return False
        last = self.store.latest_report("status")
        if last is None or sorted(g.fingerprint for g in groups) != last.meta.get("fingerprints"):
            return True
        worst = max(groups, key=lambda g: rank(g.latest.severity)).latest.severity.value
        interval = policy.reannounce_after.get(worst)
        return bool(interval) and (now - last.generated_at).total_seconds() >= interval

    async def _report(self, result: StatusResult, policy: HeartbeatPolicy, now: datetime) -> Report | None:
        assert self.narrator is not None
        groups = self.reportable_groups(result, policy)
        if not self.report_due(groups, policy, now):
            return None
        report = await self.narrator.report("status", groups=groups)
        await self.deliver(report)
        return report


class AnalysisCycle:
    """One analysis tick: signal rules against Prometheus, hits into the recommendation engine.

    With no Prometheus client or no rules file it does nothing (and says so). It never raises for a
    Prometheus outage or a bad rules file: an outage makes the rules unknown for this tick, and the
    engine doesn't treat unknown as cleared, so nothing is retired on its account.
    """

    def __init__(
        self,
        store: Store,
        *,
        rules: SignalRulesFile | None = None,
        client: PrometheusClient | None = None,
        approvals: StandingApprovals | None = None,
        tariff: TariffFile | None = None,
        tz: tzinfo | None = None,
        clock: Clock = utc_now,
    ) -> None:
        self.store = store
        self.rules = rules or SignalRulesFile(None)
        self.client = client
        self.tariff = tariff or TariffFile(None)
        self.tz = tz or ZoneInfo("UTC")
        self.clock = clock
        self.state = SignalState(store)
        self.engine = RecommendationEngine(store, approvals, clock=clock)
        self.last_run: datetime | None = None
        self.last: CycleReport | None = None

    async def __call__(self) -> CycleReport | None:
        now = self.last_run = self.clock()
        if self.client is None or not self.rules.path:
            log.info("analysis.skipped", reason="no prometheus_url" if self.client is None else "no signals file")
            return None
        ruleset = self.rules.get()
        if ruleset.file_error:
            log.warning("analysis.rules_unreadable", path=self.rules.path, error=ruleset.file_error)
            return None
        hits = await evaluate(ruleset.rules, self.client, now, state=self.state)
        tariff = self.tariff.get()
        hits = hits.merge(
            await evaluate_tariff(
                ruleset.rules, self.client, tariff, now, tz=self.tz, state=self.state, tariff_error=self.tariff.error
            )
        )
        report = self.engine.run_cycle(hits)
        if report.unknown:
            log.warning("analysis.rules_unknown", rules=sorted(report.unknown))
        self.last = report
        return report


class BriefingJob:
    """A scheduled briefing of one ``kind``: write it (model, or template when the model can't), store it, and
    hand it to :func:`deliver`. With no narrator it only records the run."""

    def __init__(
        self,
        kind: str,
        store: Store,
        *,
        narrator: Narrator | None = None,
        deliver: Deliver | None = None,
        clock: Clock = utc_now,
    ) -> None:
        self.kind = kind
        self.store = store
        self.narrator = narrator
        self.deliver: Deliver = deliver or _NO_DELIVERY
        self.clock = clock
        self.runs: list[datetime] = []
        self.last: Report | None = None

    async def __call__(self) -> Report | None:
        at = self.clock()
        self.runs.append(at)
        log.info("briefing.due", kind=self.kind, at=at.isoformat())
        if self.narrator is None:
            return None
        report = self.last = await self.narrator.report(self.kind)
        log.info("briefing.stored", kind=self.kind, report_id=report.id, path=report.meta.get("path"))
        await self.deliver(report)
        return report


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
    prometheus: PrometheusClient | None = None,
    tariff: TariffFile | None = None,
    budget: DailyBudget | None = None,
    narrator: Narrator | None = None,
    deliver: Deliver | None = None,
    dispatcher: Dispatcher | None = None,
) -> None:
    """Register the status cycle, the analysis cycle, one job per briefing and one per polling source, and with a
    ``dispatcher``, the redelivery sweep (:class:`~argus_scout.channels.redeliver.RedeliveryJob`).

    Job names are ``status``, ``analysis``, ``redeliver``, ``briefing:<kind>`` (``briefing:<kind>@HH:MM`` when a kind is
    scheduled more than once) and ``poll:<source>``. The name keys the persisted bookkeeping, so keep it
    stable. A source whose config doesn't bind gets no job (``build_sources`` logs why); the rest still do.
    ``prometheus`` overrides the client built from ``services.prometheus_url`` (a seam for tests); ``tariff``
    is the runtime's :class:`TariffFile` (one is made from ``files.tariff`` if not given), shared by the
    analysis cycle and the narrative. ``narrator`` overrides the one built from the config (its model calls
    are charged to ``budget``). ``deliver`` is handed every stored report (default: :func:`deliver`, a no-op
    until channels land).
    """
    schedule = config.schedule
    clock = scheduler.clock
    inbox = inbox or Inbox(store, clock=clock)
    sources = sources if sources is not None else build_sources(config)
    tariff = tariff if tariff is not None else TariffFile(config.files.tariff)
    narrator = narrator or build_narrator(config, store, budget, tariff=tariff, clock=clock)
    status = StatusCycle(
        store, policy_file, inbox=inbox, tz=schedule.tz, clock=clock, narrator=narrator, deliver=deliver
    )
    scheduler.add("status", status, Every(schedule.status_interval))
    if prometheus is None and config.services.prometheus_url:
        prometheus = PrometheusClient(config.services.prometheus_url)
    analysis = AnalysisCycle(
        store,
        rules=SignalRulesFile(config.files.signals),
        client=prometheus,
        approvals=StandingApprovals(config.files.approvals, store=store),
        tariff=tariff,
        tz=schedule.tz,
        clock=clock,
    )
    scheduler.add("analysis", analysis, Every(schedule.analysis_interval))
    kinds = Counter(b.kind for b in schedule.briefings)
    for briefing in schedule.briefings:
        name = f"briefing:{briefing.kind}" + (f"@{briefing.at}" if kinds[briefing.kind] > 1 else "")
        job = BriefingJob(briefing.kind, store, narrator=narrator, deliver=deliver, clock=clock)
        scheduler.add(name, job, DailyAt(briefing.at_time, schedule.tz))
    for source in sources.polling:
        poll = PollJob(source, inbox)
        scheduler.add(f"poll:{source.name}", poll, Every(poll.interval), retry_delay=min(60.0, poll.interval))
    if dispatcher is not None:
        redeliver = RedeliveryJob(store, dispatcher, config.delivery, clock=clock)
        scheduler.add(REDELIVER_JOB, redeliver, Every(config.delivery.retry_interval))


__all__ = ["AnalysisCycle", "BriefingJob", "PollJob", "StatusCycle", "StatusResult", "deliver", "register_jobs"]
