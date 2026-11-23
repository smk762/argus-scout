"""The jobs the scheduler runs: the status cycle, the analysis cycle and the briefings.

These are the seams later phases fill in. The structure is already the real one:

- :class:`StatusCycle` reloads the heartbeat policy through :class:`~argus_scout.heartbeat.PolicyFile` on
  every run, so a policy edit applies on the next tick and **a bad edit degrades to defaults while the
  cycle keeps running**. It reads open events from :meth:`Inbox.open_grouped <argus_scout.inbox.Inbox.open_grouped>`,
  skips rest hours, and decides each group through :func:`~argus_scout.heartbeat.explain`. When groups pass
  :func:`~argus_scout.heartbeat.report_gate` (capped at ``report.max_items``, most severe first) and at
  least one of them is news (:meth:`StatusCycle.is_news`: not yet announced in its current form, or its
  ``reannounce`` interval has passed), it has the :class:`~argus_scout.narrative.Narrator` write a
  ``status`` report, marks the groups announced, and hands the report to :func:`deliver`. The report's
  spoken form (``meta.speakable``) is built from the groups the voice policy allows, and is ``False`` when
  none is, so a text-only or unspeakable item is never spoken inside a status report.
- :class:`PollJob` polls one polling source and ingests what it returns through
  :meth:`Inbox.ingest <argus_scout.inbox.Inbox.ingest>`. Each source is its own job, so one failing source
  never affects another. With the source's ``resolve_missing`` on, a successful, complete poll also resolves
  the source's events it no longer lists (a cleared condition); a failed or partial poll resolves nothing.
- :class:`AnalysisCycle` reloads the signal rules through :class:`~argus_scout.signals.SignalRulesFile`,
  evaluates them against Prometheus and hands the hits to the
  :class:`~argus_scout.engine.RecommendationEngine`. A bad rule is skipped; a file that won't parse skips
  the cycle (keeping the pending ``for`` clocks); a Prometheus outage makes rules *unknown*, which the
  engine never reads as cleared. Tariff-aware rules (:mod:`~argus_scout.tariff_rules`) in the same file are
  evaluated against the :class:`~argus_scout.tariff.TariffFile` and merged in; no tariff skips just them.
- :class:`BriefingJob` generates, stores and hands to :func:`deliver` one scheduled report (``morning``,
  ``evening`` or ``status``). With no model, or the model down or over its cap, the report comes from the
  template: a briefing is never skipped. It writes through :class:`Briefings`, the one path a report written
  on request (``POST /api/reports``) takes too, so both get the same narrator, caps, GPU lease and template
  fallback, and **one report of a kind is written at a time**: a scheduled job waits for an on-request report
  of its kind to finish, and an on-request report is refused (:class:`ReportBusy`) while one of its kind is
  being written.

Every stored report goes to the ``deliver`` callable :func:`register_jobs` is given: the server passes
:func:`argus_scout.server.app.deliver_reports`, which sends it through the channels. The module-level
:func:`deliver` is a no-op default for tests and embedding.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, tzinfo
from zoneinfo import ZoneInfo

import structlog

from argus_scout.approvals import StandingApprovals
from argus_scout.budget import DailyBudget, reset_manual, set_manual
from argus_scout.channels.dispatcher import Dispatcher
from argus_scout.channels.redeliver import JOB_NAME as REDELIVER_JOB
from argus_scout.channels.redeliver import RedeliveryJob
from argus_scout.config import ScoutConfig
from argus_scout.engine import CycleReport, RecommendationEngine
from argus_scout.heartbeat import Decision, HeartbeatPolicy, PolicyFile, explain, reannounce_due
from argus_scout.inbox import EventGroup, Inbox, InvalidEvent
from argus_scout.narrative import TITLES, Narrator, build_narrator
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

Deliver = Callable[[Report], Awaitable[object]]
"""Hands a stored report to the channels; whatever it returns (the server's: the dispatch report) is passed back
by :meth:`Briefings.write`."""


async def deliver(report: Report) -> None:
    """The default ``deliver``: does nothing. ``BriefingJob`` and ``StatusCycle`` take a ``deliver`` callable, and
    the server injects one that sends through the channels (:func:`argus_scout.server.app.deliver_reports`)."""


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

    def is_news(self, group: EventGroup, policy: HeartbeatPolicy, now: datetime) -> bool:
        """Whether ``group`` is worth a status report on its own account.

        It is news when none of its events has been announced in its current form. The inbox clears an
        event's announce state whenever it becomes news again: first seen, a changed fingerprint (an
        escalation, or any change of severity or summary) or a condition that cleared and came back
        (:attr:`~argus_scout.inbox.Outcome.RECURRED`). A fresh event id repeating an announced group's
        fingerprint is not news. An announced group is news again once its severity's ``reannounce``
        interval has passed since it was last announced (:func:`~argus_scout.heartbeat.reannounce_due`)."""
        if not any(e.last_broadcast_at for e in group.events):
            return True
        return reannounce_due(group.events, policy, now)

    def report_due(self, groups: list[EventGroup], policy: HeartbeatPolicy, now: datetime) -> bool:
        """Whether a status report of these groups is due: at least one of them is news (:meth:`is_news`). Groups
        already announced ride along as context, so an item clearing (A, B down to A) is not itself news."""
        return any(self.is_news(g, policy, now) for g in groups)

    def speech(
        self, groups: list[EventGroup], decisions: dict[str, Decision], policy: HeartbeatPolicy, now: datetime
    ) -> str | bool:
        """What the report says aloud: each group's spoken text (its latest event's ``speech``, else its summary) for
        the groups the voice policy allows
        (:func:`~argus_scout.heartbeat.voice_gate` via ``Decision.voice``: the source's routing, ``speakable``,
        ``voice.min_severity``), or ``False`` when there are none, so the report goes to text channels only.
        With ``voice.on_change_only`` (the default) a group already announced is left out unless it is news
        again (:meth:`is_news`): repeats stay text-only."""
        spoken = [
            g.latest.spoken.strip().rstrip(".")
            for g in groups
            if decisions[g.fingerprint].voice and (not policy.voice_on_change_only or self.is_news(g, policy, now))
        ]
        if not spoken:
            return False
        return " ".join([f"{TITLES['status']}.", *(s if s.endswith(("!", "?")) else f"{s}." for s in spoken)])

    async def _report(self, result: StatusResult, policy: HeartbeatPolicy, now: datetime) -> Report | None:
        assert self.narrator is not None
        groups = self.reportable_groups(result, policy)
        if not self.report_due(groups, policy, now):
            return None
        decisions = {g.fingerprint: d for g, d in zip(result.groups, result.decisions, strict=True)}
        speakable = self.speech(groups, decisions, policy, now)
        report = await self.narrator.report("status", groups=groups, meta={"speakable": speakable})
        for group in groups:
            for event in group.events:
                self.store.mark_announced(event.source, event.event_id, at=now)
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


class ReportBusy(Exception):
    """A report of this kind is being written right now (scheduled or on request); nothing was written."""

    def __init__(self, kind: str) -> None:
        super().__init__(f"a {kind} report is being written right now; try again when it is done")
        self.kind = kind


@dataclass
class Written:
    """What :meth:`Briefings.write` did: the stored report, and what ``deliver`` returned (None if not delivered)."""

    report: Report
    delivered: bool = False
    delivery: object = None


class Briefings:
    """The one path a briefing is written by, scheduled (:class:`BriefingJob`) or on request (``POST /api/reports``).

    :meth:`write` has the narrator compose and store the report (the model, charged to its daily cap and under its
    GPU lease, or the template when the model can't), then hands it to ``deliver``. One report of a kind is written
    at a time, so two can't race for the model or go out twice: ``wait=True`` (the scheduler) queues behind one in
    progress; ``wait=False`` (a request) raises :class:`ReportBusy` instead of holding the caller for a model call.

    **Held to the daily caps, whoever asked.** :meth:`write` runs as background work for the budget even while
    serving an HTTP request (which otherwise counts as manual and may go over a cap): an endpoint anyone with the
    token can call in a loop is what the caps are for. Over ``llm_calls`` the report comes from the template; over
    the speech caps, voice is held back. ``budget.block_manual`` doesn't change this."""

    def __init__(self, narrator: Narrator, *, deliver: Deliver | None = None) -> None:
        self.narrator = narrator
        self.deliver: Deliver = deliver or _NO_DELIVERY
        self._locks: dict[str, asyncio.Lock] = {}
        self._pending: Counter[str] = Counter()

    def busy(self, kind: str) -> bool:
        """Whether a report of ``kind`` is being written or waiting to be."""
        return self._pending[kind] > 0

    async def write(
        self, kind: str, *, use_llm: bool = True, trigger: str = "scheduled", deliver: bool = True, wait: bool = True
    ) -> Written:
        if not wait and self.busy(kind):
            raise ReportBusy(kind)
        lock = self._locks.setdefault(kind, asyncio.Lock())
        self._pending[kind] += 1  # before any await, so the check above and this are one step on the loop
        manual = set_manual(False)
        try:
            async with lock:
                report = await self.narrator.report(kind, use_llm=use_llm, trigger=trigger)
                log.info(
                    "briefing.stored", kind=kind, report_id=report.id, path=report.meta.get("path"), trigger=trigger
                )
                if not deliver:
                    return Written(report)
                return Written(report, delivered=True, delivery=await self.deliver(report))
        finally:
            reset_manual(manual)
            self._pending[kind] -= 1


class BriefingJob:
    """A scheduled briefing of one ``kind``: written through :class:`Briefings` (model, or template when the model
    can't), stored, and handed to :func:`deliver`. With no narrator it only records the run. ``briefings`` is the
    runtime's shared writer (so on-request reports and this job don't overlap); without one, a private writer is
    made from ``narrator`` and ``deliver``."""

    def __init__(
        self,
        kind: str,
        store: Store,
        *,
        narrator: Narrator | None = None,
        deliver: Deliver | None = None,
        briefings: Briefings | None = None,
        clock: Clock = utc_now,
    ) -> None:
        self.kind = kind
        self.store = store
        if briefings is None and narrator is not None:
            briefings = Briefings(narrator, deliver=deliver)
        self.briefings = briefings
        self.clock = clock
        self.runs: list[datetime] = []
        self.last: Report | None = None

    async def __call__(self) -> Report | None:
        at = self.clock()
        self.runs.append(at)
        log.info("briefing.due", kind=self.kind, at=at.isoformat())
        if self.briefings is None:
            return None
        written = await self.briefings.write(self.kind, trigger="scheduled", wait=True)
        self.last = written.report
        return written.report


class PollJob:
    """Poll one source and ingest what it returns. A failed poll raises, so the scheduler counts and retries
    it; an event the inbox refuses is logged and skipped, and the rest of the batch still lands.

    When the source config's ``resolve_missing`` is true (json_poll's default; a plugin opts in), the poll is
    the source's whole current state, so its events missing from it are resolved through
    :meth:`Inbox.resolve_missing <argus_scout.inbox.Inbox.resolve_missing>`: closed by scout, never acked as a
    person. Only after a poll that returned, and only if the result isn't marked incomplete
    (:class:`~argus_scout.sources.base.PollResult`): a failed or partial poll must never clear anything."""

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
        if getattr(self.source.config, "resolve_missing", False):
            if getattr(events, "complete", True):
                present = [str(e.event_id or "").strip() for e in events]
                batch["resolved"] += len(self.inbox.resolve_missing(self.source.name, present))
            else:
                log.warning("poll.resolve_skipped", source=self.source.name, reason="incomplete poll")
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
    briefings: Briefings | None = None,
    approvals: StandingApprovals | None = None,
) -> Briefings:
    """Register the status cycle, the analysis cycle, one job per briefing and one per polling source, and with a
    ``dispatcher``, the redelivery sweep (:class:`~argus_scout.channels.redeliver.RedeliveryJob`).

    Job names are ``status``, ``analysis``, ``redeliver``, ``briefing:<kind>@HH:MM`` and ``poll:<source>``. The name keys the
    persisted bookkeeping, so keep it stable: a briefing's name depends only on its own kind and time, never on
    how many others share the kind (adding one must not rename, and so re-fire, another). A source whose config doesn't bind gets no job (``build_sources`` logs why); the rest still do.
    ``prometheus`` overrides the client built from ``services.prometheus_url`` (a seam for tests); ``tariff``
    is the runtime's :class:`TariffFile` (one is made from ``files.tariff`` if not given), shared by the
    analysis cycle and the narrative. ``narrator`` overrides the one built from the config (its model calls
    are charged to ``budget``). ``deliver`` is handed every stored report (default: :func:`deliver`, a no-op).
    ``briefings`` is the shared report writer (its narrator and ``deliver`` then win); one is made if not given.
    Returns it, so on-request reports go through the same writer as the scheduled ones.
    ``approvals`` is the runtime's :class:`StandingApprovals`, shared with the HTTP API so a rule it adds applies
    from the next analysis cycle (one is made from ``files.approvals`` if not given).
    """
    schedule = config.schedule
    clock = scheduler.clock
    inbox = inbox or Inbox(store, clock=clock)
    sources = sources if sources is not None else build_sources(config)
    tariff = tariff if tariff is not None else TariffFile(config.files.tariff)
    if briefings is not None:
        narrator, deliver = briefings.narrator, briefings.deliver
    if prometheus is None and config.services.prometheus_url:
        prometheus = PrometheusClient(config.services.prometheus_url)
    narrator = narrator or build_narrator(config, store, budget, tariff=tariff, clock=clock)
    if briefings is None:
        briefings = Briefings(narrator, deliver=deliver)
    status = StatusCycle(
        store, policy_file, inbox=inbox, tz=schedule.tz, clock=clock, narrator=narrator, deliver=deliver
    )
    scheduler.add("status", status, Every(schedule.status_interval))
    analysis = AnalysisCycle(
        store,
        rules=SignalRulesFile(config.files.signals),
        client=prometheus,
        approvals=approvals if approvals is not None else StandingApprovals(config.files.approvals, store=store),
        tariff=tariff,
        tz=schedule.tz,
        clock=clock,
    )
    scheduler.add("analysis", analysis, Every(schedule.analysis_interval))
    for briefing in schedule.briefings:
        name = f"briefing:{briefing.kind}@{briefing.at}"
        job = BriefingJob(briefing.kind, store, briefings=briefings, clock=clock)
        scheduler.add(name, job, DailyAt(briefing.at_time, schedule.tz))
    for source in sources.polling:
        poll = PollJob(source, inbox)
        scheduler.add(f"poll:{source.name}", poll, Every(poll.interval), retry_delay=min(60.0, poll.interval))
    if dispatcher is not None:
        redeliver = RedeliveryJob(store, dispatcher, config.delivery, clock=clock)
        scheduler.add(REDELIVER_JOB, redeliver, Every(config.delivery.retry_interval))
    return briefings


__all__ = [
    "AnalysisCycle",
    "BriefingJob",
    "Briefings",
    "PollJob",
    "ReportBusy",
    "StatusCycle",
    "StatusResult",
    "Written",
    "deliver",
    "register_jobs",
]
