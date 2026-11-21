"""The recommendation engine: signal hits in, a maintained set of recommendations out.

One :meth:`RecommendationEngine.run_cycle` per analysis cycle. For each hit (see
:mod:`argus_scout.signals`), keyed by its dedup key (rule + ``labels_key`` values):

- **Dedup.** An open recommendation (draft, proposed or approved) with the same key is *refreshed*:
  its evidence, ``last_seen_at`` and ``expires_at`` move on, its severity is raised (never lowered), and
  a proposal's text is re-rendered. An approved one keeps the wording that was approved. Nothing is
  ever duplicated.
- **Rejection memory.** If there's no open one, but a person rejected this key before, the hit is
  *suppressed*, unless its severity is above the severity that was rejected (an escalation is new
  news), or the rule sets a ``rejection_ttl`` and that long has passed since the rejection. Without a
  ``rejection_ttl`` a rejection holds until the signal escalates. :meth:`recent_rejections` lists
  rejections and their reasons for the narrative to quote.
- **New.** Otherwise a new recommendation is created and moved to ``proposed``, and standing
  approvals are applied right then, through :class:`StandingApprovals` and its guardrails: a proposal
  with high impact, or raised from a critical signal, is never auto-approved.

Then **expiry**: a *proposed* recommendation is retired, with the reason in the audit trail, when its
rule was evaluated and the instance has been absent for longer than ``clear_grace``, or when it is past
``expires_at`` (``expires_after`` on the rule, else ``default_ttl``, counted from when the signal was
last seen). A rule that couldn't be evaluated (Prometheus unknown) never clears anything; only
``expires_at`` can retire its proposals, so a long outage can't keep stale ones open forever.
Approved recommendations are left alone: a person (or a standing rule) said yes, and only a person
moves one further.

**Advisory only.** The engine moves recommendations to ``proposed``, to ``approved`` (by standing
rule only), and from ``proposed`` to ``retired``. It never sets ``deployed`` and never acts.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import structlog

from argus_scout.approvals import StandingApprovals
from argus_scout.recommendations import Recommendation, Status
from argus_scout.severity import Severity, rank
from argus_scout.signals import Evaluation, SignalHit
from argus_scout.store.base import Store, StoreConflict

log = structlog.get_logger()

OPEN = (Status.DRAFT, Status.PROPOSED, Status.APPROVED)
MAX_EVIDENCE = 10
ENGINE = "engine"


@dataclass(frozen=True)
class Rejection:
    """A rejected recommendation, as the narrative quotes it."""

    rec_id: str
    key: str
    title: str
    reason: str
    severity: Severity
    rejected_at: datetime


@dataclass
class CycleReport:
    """What one cycle did."""

    new: list[Recommendation] = field(default_factory=list)
    refreshed: list[Recommendation] = field(default_factory=list)
    auto_approved: list[Recommendation] = field(default_factory=list)
    retired: list[Recommendation] = field(default_factory=list)
    suppressed: list[SignalHit] = field(default_factory=list)
    """Hits not raised because their key was rejected (and hasn't escalated or aged out)."""
    unknown: dict[str, str] = field(default_factory=dict)
    """Rules that couldn't be evaluated this cycle, and why. Unknown, not clear."""

    @property
    def changed(self) -> bool:
        return bool(self.new or self.refreshed or self.auto_approved or self.retired)

    def lines(self) -> list[str]:
        """A plain-text account, one line per item."""
        out = []
        for rec in self.new:
            out.append(f"new [{rec.severity.value}] {rec.title} ({rec.status.value})")
        for rec in self.auto_approved:
            out.append(f"auto-approved by rule {rec.approved_by_rule}: {rec.title} (approved only, not deployed)")
        for rec in self.refreshed:
            out.append(f"refreshed [{rec.severity.value}] {rec.title} ({rec.status.value})")
        for rec in self.retired:
            out.append(f"retired {rec.title}: {rec.retired_reason}")
        for hit in self.suppressed:
            out.append(f"suppressed (previously rejected) {hit.title}")
        for rule, error in sorted(self.unknown.items()):
            out.append(f"unknown {rule}: {error}")
        return out

    def summary(self) -> str:
        return (
            f"{len(self.new)} new, {len(self.refreshed)} refreshed, {len(self.auto_approved)} auto-approved, "
            f"{len(self.retired)} retired, {len(self.suppressed)} suppressed by rejection, "
            f"{len(self.unknown)} rules unknown"
        )


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _worst(hits: Iterable[SignalHit]) -> dict[str, SignalHit]:
    """One hit per key: the most severe (any evaluator may repeat a key)."""
    by_key: dict[str, SignalHit] = {}
    for hit in hits:
        prev = by_key.get(hit.key)
        if prev is None or rank(hit.severity) > rank(prev.severity):
            by_key[hit.key] = hit
    return by_key


class RecommendationEngine:
    """Maintains recommendations from signal hits. See the module docstring for every decision."""

    def __init__(
        self,
        store: Store,
        approvals: StandingApprovals | None,
        *,
        clock: Callable[[], datetime] = _utc_now,
        clear_grace: timedelta = timedelta(hours=1),
        default_ttl: timedelta = timedelta(days=7),
    ) -> None:
        self.store = store
        self.approvals = approvals
        self.clock = clock
        self.clear_grace = clear_grace
        self.default_ttl = default_ttl

    # -- the cycle -------------------------------------------------------------------------------------------
    def run_cycle(self, hits: Iterable[SignalHit], *, evaluated: Iterable[str] | None = None) -> CycleReport:
        """Apply one evaluation's hits. ``hits`` is usually an :class:`Evaluation`.

        ``evaluated`` names the rules whose queries were answered, so their absent instances count as
        cleared. It defaults to the evaluation's own ``evaluated``; for a plain list of hits, only the
        rules that produced a hit count, so a rule with no hits is never taken to be all clear.
        """
        now = self.clock()
        report = CycleReport()
        if isinstance(hits, Evaluation):
            report.unknown = dict(hits.unknown)
            if evaluated is None:
                evaluated = hits.evaluated
        by_key = _worst(hits)
        ran = set(evaluated) if evaluated is not None else {hit.rule for hit in by_key.values()}
        ran -= set(report.unknown)

        for key, hit in by_key.items():
            open_recs = self.store.recommendations_by_key(key, OPEN)
            if open_recs:
                if self._refresh(open_recs[0], hit, now):
                    report.refreshed.append(open_recs[0])
                continue
            if self._suppressed(hit, now):
                report.suppressed.append(hit)
                continue
            rec = self._propose(hit, now)
            report.new.append(rec)
            if rec.status == Status.APPROVED and rec.approval_source == "standing":
                report.auto_approved.append(rec)

        for rec in self.store.list_recommendations(Status.PROPOSED):
            if rec.key in by_key:
                continue
            reason = self._expiry_reason(rec, ran, now)
            if reason and rec.retire(reason, now=now) and self._save(rec, Status.PROPOSED, by=ENGINE, reason=reason):
                report.retired.append(rec)

        log.info("engine.cycle", summary=report.summary())
        return report

    # -- steps -------------------------------------------------------------------------------------------------
    def _expires_at(self, hit: SignalHit, now: datetime) -> datetime:
        return now + (hit.expires_after or self.default_ttl)

    def _save(self, rec: Recommendation, loaded: Status | None, **kwargs: str | None) -> bool:
        """Save ``rec`` if its stored status is still ``loaded``. A person may have decided it since this cycle
        read it: then the write is refused, logged and skipped, and the next cycle sees the decision."""
        try:
            self.store.save_recommendation(rec, expected_status=loaded, **kwargs)
        except StoreConflict as exc:
            log.warning("engine.stale_skipped", rec_id=rec.id, key=rec.key, error=str(exc))
            return False
        return True

    def _refresh(self, rec: Recommendation, hit: SignalHit, now: datetime) -> bool:
        loaded = rec.status
        if rec.status in (Status.DRAFT, Status.PROPOSED):
            rec.title, rec.action, rec.rationale = hit.title, hit.action, hit.rationale
            rec.impact, rec.confidence = hit.impact, hit.confidence
        if rank(hit.severity) > rank(rec.severity):
            rec.severity = hit.severity
        rec.evidence = [*rec.evidence, hit.evidence][-MAX_EVIDENCE:]
        rec.last_seen_at = now
        rec.expires_at = self._expires_at(hit, now)
        rec.updated_at = now
        return self._save(rec, loaded, by=ENGINE)

    def _suppressed(self, hit: SignalHit, now: datetime) -> bool:
        rejected = self.store.recommendations_by_key(hit.key, Status.REJECTED)
        if not rejected:
            return False
        last = rejected[0]
        if rank(hit.severity) > rank(last.severity):
            log.info("engine.rejection_escalated", key=hit.key, was=last.severity.value, now=hit.severity.value)
            return False
        if hit.rejection_ttl is not None and now - last.updated_at >= hit.rejection_ttl:
            log.info("engine.rejection_aged_out", key=hit.key, rejected_at=last.updated_at.isoformat())
            return False
        return True

    def _propose(self, hit: SignalHit, now: datetime) -> Recommendation:
        evidence = [hit.evidence]
        rejected = self.store.recommendations_by_key(hit.key, Status.REJECTED)
        if rejected:
            last = rejected[0]
            note = f"previously rejected at {last.severity.value} on {last.updated_at:%Y-%m-%d}"
            evidence.insert(0, f"{note}: {last.rejection_reason}" if last.rejection_reason else note)
        rec = Recommendation(
            title=hit.title,
            action=hit.action,
            rationale=hit.rationale,
            impact=hit.impact,
            confidence=hit.confidence,
            analyzer=hit.rule,
            severity=hit.severity,
            evidence=evidence,
            key=hit.key,
            created_at=now,
            updated_at=now,
            last_seen_at=now,
            expires_at=self._expires_at(hit, now),
        )
        self.store.save_recommendation(rec, by=ENGINE, expected_status=None)
        rec.transition(Status.PROPOSED, now=now)
        self.store.save_recommendation(rec, by=ENGINE, expected_status=Status.DRAFT)
        if self.approvals is not None and self.approvals.apply(rec, now) is not None:
            # The transition records rule:<id> as who approved it. A person who decided first wins.
            if self._save(rec, Status.PROPOSED):
                log.info("engine.auto_approved", key=rec.key, rule=rec.approved_by_rule)
            else:
                rec = self.store.get_recommendation(rec.id) or rec
        return rec

    def _expiry_reason(self, rec: Recommendation, ran: set[str], now: datetime) -> str | None:
        if rec.expires_at is not None and now >= rec.expires_at:
            return f"expired: unanswered since {rec.created_at:%Y-%m-%d %H:%M}Z, past its expiry"
        if rec.key and rec.analyzer in ran:
            seen = rec.last_seen_at or rec.updated_at
            if now - seen > self.clear_grace:
                return f"signal cleared: not seen since {seen:%Y-%m-%d %H:%M}Z"
        return None

    # -- queries -----------------------------------------------------------------------------------------------
    def recent_rejections(self, limit: int = 10) -> list[Rejection]:
        """The most recent rejections, newest first, with the reasons people gave."""
        return [
            Rejection(
                rec_id=rec.id,
                key=rec.key,
                title=rec.title,
                reason=rec.rejection_reason,
                severity=rec.severity,
                rejected_at=rec.updated_at,
            )
            for rec in self.store.recent_rejections(limit)
        ]


__all__ = ["CycleReport", "RecommendationEngine", "Rejection"]
