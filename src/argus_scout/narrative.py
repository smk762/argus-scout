"""The narrative: morning, evening and status reports written from structured facts.

Three steps, each its own function so it can be tested and explained:

1. **Facts** (:meth:`Narrator.gather`). Everything a report may say, read from scout's own state: open
   inbox items (grouped by fingerprint), recommendations awaiting a decision and approved ones, recent
   rejections with the reasons people gave, the tariff period and the next change, and the operator's
   plain-file notes (:mod:`argus_scout.memory`). Nothing else reaches the model, so nothing else can be
   claimed.
2. **Prompt** (:func:`build_prompt`). The facts become lines, each with a priority *tier* (critical items
   first, then warnings, the rejections, notices, info, and notes last). The prompt is sized for a small
   local model: ``context_tokens`` (default 3072) minus the output allowance (``max_tokens``), the system
   prompt and the instructions, estimated at four characters a token, is what's left for facts. Lines are
   taken in priority order until the next one doesn't fit; every line not taken is counted, and the
   prompt says how many were left out per section. So the cut always falls on the least severe facts.
3. **Text.** The prompt goes to the model through :class:`~argus_scout.llm.TextGenerator` (charged to
   the ``llm_calls`` daily cap). **If there is no model, it is over its cap, or it fails, the report is
   written from a deterministic template** (:func:`render_template`) from the same facts, so a briefing
   is never silently skipped. The report's ``meta.path`` says which ran (``llm`` or ``template``) and
   ``meta.fallback_reason`` why.

Every report is saved to the store with ``meta`` recording the facts used (event, recommendation and
rejection ids, the tariff period, note names, what was left out), the persona and the model.

**Advisory only.** The model's text is stored and shown, never parsed. A report can describe and
recommend; nothing in it moves a recommendation or starts anything.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo

import structlog

from argus_scout.budget import DailyBudget
from argus_scout.config import ScoutConfig
from argus_scout.engine import RecommendationEngine, Rejection
from argus_scout.inbox import EventGroup, Inbox
from argus_scout.llm import (
    CHARS_PER_TOKEN,
    LLM_CALLS,
    LLMUnavailable,
    Prompt,
    TextGenerator,
    build_backend,
    estimate_tokens,
)
from argus_scout.memory import Memory, MemoryPathError, Note, render_rejection
from argus_scout.persona import Persona, PersonaFile
from argus_scout.recommendations import Recommendation, Status
from argus_scout.scheduler import Clock, utc_now
from argus_scout.severity import Severity, rank
from argus_scout.store.base import Report, Store
from argus_scout.tariff import TariffFile, current_period, next_period_change

log = structlog.get_logger()

ReportKind = Literal["morning", "evening", "status"]
REPORT_KINDS: tuple[str, ...] = ("morning", "evening", "status")

MAX_LINE_CHARS = 300
"""One fact line is clipped here, so a single long summary can't crowd out the rest."""
TEMPLATE_MAX_PER_SECTION = 10
PROMPT_MARGIN = 0.9
"""Use 90% of the computed fact budget: chars/4 is an estimate, and small models fail badly when over."""

SECTIONS: dict[str, str] = {
    "now": "Now",
    "items": "Open status items",
    "proposed": "Recommendations awaiting a decision",
    "approved": "Approved recommendations (approved only: a person carries them out)",
    "rejected": "Rejected by a person: do not propose again or argue with the reason",
    "tariff": "Energy tariff",
    "notes": "Operator notes",
}
"""Section keys and headings, in the order they appear in a prompt."""

KIND_SECTIONS: dict[str, tuple[str, ...]] = {
    "morning": tuple(SECTIONS),
    "evening": tuple(SECTIONS),
    "status": ("now", "items", "rejected"),
}

INSTRUCTIONS: dict[str, str] = {
    "morning": (
        "Write the morning briefing: what needs attention today, recommendations awaiting a decision, and the "
        "energy period ahead. If nothing needs attention, say so in one sentence. Under 150 words."
    ),
    "evening": (
        "Write the evening review: what is still open, what was approved, and what to watch overnight. "
        "If nothing needs attention, say so in one sentence. Under 150 words."
    ),
    "status": (
        "Write a short status update covering only the listed status items, most severe first. "
        "Do not add anything that isn't listed. Under 80 words."
    ),
}

TITLES = {"morning": "Morning briefing", "evening": "Evening review", "status": "Status update"}

# Priority tiers: lower is kept first when the prompt has to be cut.
_SEVERITY_TIER = {Severity.CRITICAL: 1, Severity.WARNING: 2, Severity.NOTICE: 4, Severity.INFO: 5}
_REJECTION_TIER = 3
_NOTE_TIER = 6


def _clip(text: str, limit: int = MAX_LINE_CHARS) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _duration(seconds: int) -> str:
    hours, minutes = divmod(max(0, seconds) // 60, 60)
    return f"{hours}h{minutes:02d}m" if hours else f"{minutes}m"


# --- facts --------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class TariffFact:
    period: str
    rate: float
    unit: str
    currency: str
    next_period: str
    next_in_seconds: int

    def line(self) -> str:
        now = f"{self.period} period now, {self.rate:g} {self.currency} minor units per {self.unit}"
        if self.next_period == self.period:
            return f"{now}, all day."
        return f"{now}; {self.next_period} starts in {_duration(self.next_in_seconds)}."

    def to_dict(self) -> dict[str, Any]:
        return {"period": self.period, "rate": self.rate, "next": self.next_period, "next_in": self.next_in_seconds}


@dataclass
class Facts:
    """Everything one report may draw on."""

    kind: str
    at: datetime
    """When, in the schedule's timezone."""
    groups: list[EventGroup] = field(default_factory=list)
    proposed: list[Recommendation] = field(default_factory=list)
    approved: list[Recommendation] = field(default_factory=list)
    rejections: list[Rejection] = field(default_factory=list)
    tariff: TariffFact | None = None
    notes: list[Note] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        """Nothing to report: no open items (and, outside a status update, no recommendations)."""
        if self.kind == "status":
            return not self.groups
        return not (self.groups or self.proposed or self.approved)


@dataclass(frozen=True)
class Line:
    section: str
    text: str
    tier: int
    ref: str = ""
    """What the line is about (event id, recommendation id, note name), for the report's meta."""


def _item_line(group: EventGroup) -> str:
    event = group.latest
    text = f"[{event.severity.value}] {event.source}: {event.summary}"
    if group.count > 1:
        text += f" (reported {group.count} times)"
    if event.requires_ack:
        text += " (needs an acknowledgement)"
    return text


def _rec_line(rec: Recommendation) -> str:
    text = f"[{rec.severity.value}] {rec.title}"
    if rec.action:
        text += f": {rec.action}"
    if rec.rationale:
        text += f" (why: {rec.rationale})"
    if rec.status is Status.APPROVED and rec.approval_source == "standing":
        text += f" (approved by standing rule {rec.approved_by_rule})"
    return text


def fact_lines(facts: Facts) -> list[Line]:
    """Every fact as a prioritised line, in display order within each section (most severe first)."""
    sections = KIND_SECTIONS.get(facts.kind, tuple(SECTIONS))
    lines: list[Line] = [Line("now", f"{facts.at:%A %d %B %Y, %H:%M %Z}.", 0)]
    if "items" in sections:
        groups = sorted(facts.groups, key=lambda g: (-rank(g.latest.severity), -g.latest.generated_at.timestamp()))
        for g in groups:
            lines.append(Line("items", _item_line(g), _SEVERITY_TIER[g.latest.severity], g.latest.event_id))
    if "proposed" in sections:
        for rec in sorted(facts.proposed, key=lambda r: (-rank(r.severity), -r.updated_at.timestamp())):
            lines.append(Line("proposed", _rec_line(rec), _SEVERITY_TIER[rec.severity], rec.id))
    if "approved" in sections:
        for rec in sorted(facts.approved, key=lambda r: (-rank(r.severity), -r.updated_at.timestamp())):
            lines.append(Line("approved", _rec_line(rec), _SEVERITY_TIER[rec.severity] + 1, rec.id))
    if "rejected" in sections:
        for rej in facts.rejections:
            lines.append(Line("rejected", render_rejection(rej), _REJECTION_TIER, rej.rec_id))
    if "tariff" in sections and facts.tariff is not None:
        lines.append(Line("tariff", facts.tariff.line(), 0, facts.tariff.period))
    if "notes" in sections:
        for note in facts.notes:
            lines.append(Line("notes", f"{note.name}: {note.text}", _NOTE_TIER, note.name))
    return [Line(ln.section, _clip(ln.text, MAX_LINE_CHARS), ln.tier, ln.ref) for ln in lines]


# --- prompt -------------------------------------------------------------------------------------------------


@dataclass
class Selection:
    """Which fact lines made it into a prompt, and how many per section didn't."""

    lines: list[Line]
    omitted: dict[str, int]
    budget_chars: int


def select_lines(lines: list[Line], budget_chars: int, *, notes_chars: int | None = None) -> Selection:
    """Take lines in priority order (tier, then display order) until the next one doesn't fit.

    Stopping at the first line that doesn't fit, rather than skipping it and trying smaller ones, keeps the
    cut monotonic: everything kept is at least as important as everything left out. ``notes_chars`` caps
    the operator notes on their own, inside the overall budget.
    """
    order = sorted(range(len(lines)), key=lambda i: (lines[i].tier, i))
    used, notes_used, kept = 0, 0, set()
    for i in order:
        line = lines[i]
        cost = len(line.text) + 3  # "- " and a newline
        if line.section == "notes" and notes_chars is not None:
            if notes_used + cost > notes_chars:
                continue  # notes are last in priority; a note over its own cap drops only notes
            notes_used += cost
        if used + cost > budget_chars:
            break
        used += cost
        kept.add(i)
    omitted: dict[str, int] = {}
    for i, line in enumerate(lines):
        if i not in kept:
            omitted[line.section] = omitted.get(line.section, 0) + 1
    return Selection([lines[i] for i in sorted(kept)], omitted, budget_chars)


def render_facts(selection: Selection) -> str:
    out: list[str] = []
    for key, heading in SECTIONS.items():
        lines = [ln for ln in selection.lines if ln.section == key]
        missing = selection.omitted.get(key, 0)
        if not lines and not missing:
            continue
        out.append(f"## {heading}")
        out += [f"- {ln.text}" for ln in lines]
        if missing:
            out.append(f"- ({missing} more not shown)")
        out.append("")
    return "\n".join(out).rstrip()


def build_prompt(
    facts: Facts,
    persona: Persona,
    *,
    context_tokens: int = 3072,
    max_tokens: int = 400,
    notes_chars: int | None = 2000,
) -> tuple[Prompt, Selection]:
    """The system and user messages for ``facts``, cut to fit the model's context (see the module docstring)."""
    system = persona.system_prompt()
    instructions = INSTRUCTIONS.get(facts.kind, INSTRUCTIONS["status"])
    if facts.empty:
        instructions += " There are no open items or recommendations."
    header = f"{instructions}\n\nFacts:\n"
    fixed = len(system) + len(header)
    budget = int((context_tokens - max_tokens) * CHARS_PER_TOKEN * PROMPT_MARGIN) - fixed
    selection = select_lines(fact_lines(facts), max(0, budget), notes_chars=notes_chars)
    return Prompt(system=system, user=header + render_facts(selection)), selection


# --- the template fallback -------------------------------------------------------------------------------------


def render_template(facts: Facts, persona: Persona, *, reason: str = "") -> str:
    """A plain-text report from the facts alone, no model: deterministic, and never empty."""
    lines = fact_lines(facts)
    out = [f"{TITLES.get(facts.kind, 'Report')}, {facts.at:%a %d %b %H:%M}."]
    headings = {
        "items": "Needs attention:",
        "proposed": "Awaiting a decision:",
        "approved": "Approved, for a person to carry out:",
        "rejected": "Not raising again (rejected):",
    }
    for key, heading in headings.items():
        section = [ln for ln in lines if ln.section == key]
        if not section:
            continue
        out += ["", heading]
        out += [f"- {ln.text}" for ln in section[:TEMPLATE_MAX_PER_SECTION]]
        if len(section) > TEMPLATE_MAX_PER_SECTION:
            out.append(f"- and {len(section) - TEMPLATE_MAX_PER_SECTION} more")
    if facts.empty:
        out += ["", "Nothing needs attention." if facts.kind != "status" else "No open status items."]
    tariff = [ln for ln in lines if ln.section == "tariff"]
    if tariff:
        out += ["", f"Energy: {tariff[0].text}"]
    if persona.sign_off:
        out += ["", persona.sign_off]
    if reason:
        out += ["", f"(Written from a template: {reason}.)"]
    return "\n".join(out)


_REASON_TEXT = {
    "budget": "today's model budget is spent",
    "error": "the model was unavailable",
    "disabled": "the model was turned off for this run",
    "no_llm": "no model is configured",
}


# --- the narrator -------------------------------------------------------------------------------------------


@dataclass
class Draft:
    """A composed report, not yet stored."""

    kind: str
    at: datetime
    text: str
    meta: dict[str, Any]


class Narrator:
    """Gathers facts, builds the prompt, gets text (model or template) and stores the report."""

    def __init__(
        self,
        store: Store,
        *,
        generator: TextGenerator | None = None,
        persona: PersonaFile | None = None,
        memory: Memory | None = None,
        tariff: TariffFile | None = None,
        tz: ZoneInfo | None = None,
        clock: Clock = utc_now,
        max_tokens: int = 400,
        context_tokens: int = 3072,
        rejection_limit: int = 10,
        budget_kind: str = LLM_CALLS,
    ) -> None:
        self.store = store
        self.generator = generator
        self.persona = persona or PersonaFile(None)
        self.memory = memory or Memory(None)
        self.tariff = tariff or TariffFile(None)
        self.tz = tz or ZoneInfo("UTC")
        self.clock = clock
        self.max_tokens = max_tokens
        self.context_tokens = context_tokens
        self.rejection_limit = rejection_limit
        self.budget_kind = budget_kind

    def gather(self, kind: str, *, groups: list[EventGroup] | None = None) -> Facts:
        """The facts for a ``kind`` report. ``groups`` overrides the inbox (the status cycle passes the groups
        its heartbeat policy chose)."""
        now = self.clock()
        local = now.astimezone(self.tz)
        if groups is None:
            groups = Inbox(self.store, clock=self.clock).open_grouped()
        facts = Facts(kind=kind, at=local, groups=list(groups))
        facts.proposed = self.store.list_recommendations(Status.PROPOSED)
        facts.approved = self.store.list_recommendations(Status.APPROVED, limit=20)
        facts.rejections = RecommendationEngine(self.store, None).recent_rejections(self.rejection_limit)
        tariff = self.tariff.get()
        if tariff is not None and tariff.periods:
            period = current_period(tariff, local)
            next_name, seconds = next_period_change(tariff, local)
            facts.tariff = TariffFact(period.name, period.rate, tariff.unit, tariff.currency, next_name, seconds)
        facts.notes = self.memory.notes()
        return facts

    def _sync_memory(self, rejections: list[Rejection]) -> str | None:
        try:
            path = self.memory.write_rejections(rejections)
        except (MemoryPathError, OSError) as exc:
            log.warning("memory.write_failed", error=str(exc))
            return None
        return str(path) if path else None

    async def compose(
        self,
        kind: str,
        *,
        groups: list[EventGroup] | None = None,
        use_llm: bool = True,
        write_memory: bool = True,
    ) -> Draft:
        """Write a report without storing it. Never raises for a model problem: it falls back to the template."""
        if kind not in REPORT_KINDS:
            raise ValueError(f"unknown report kind {kind!r} (known: {', '.join(REPORT_KINDS)})")
        facts = self.gather(kind, groups=groups)
        if write_memory:
            self._sync_memory(facts.rejections)
        persona = self.persona.get()
        prompt, selection = build_prompt(
            facts,
            persona,
            context_tokens=self.context_tokens,
            max_tokens=self.max_tokens,
            notes_chars=self.memory.max_chars,
        )
        meta: dict[str, Any] = {
            "persona": persona.name,
            "persona_error": self.persona.error,
            "prompt_tokens_est": estimate_tokens(prompt.system + prompt.user),
            "context_tokens": self.context_tokens,
            "fingerprints": sorted(g.fingerprint for g in facts.groups),
        }
        reason, error = "", None
        if not use_llm:
            reason = "disabled"
        elif self.generator is None:
            reason = "no_llm"
        else:
            try:
                completion = await self.generator.generate(prompt, max_tokens=self.max_tokens, kind=self.budget_kind)
            except LLMUnavailable as exc:
                reason, error = exc.reason, str(exc)
            else:
                meta |= {
                    "path": "llm",
                    "fallback_reason": None,
                    "backend": completion.backend,
                    "model": completion.model,
                    "prompt_tokens": completion.prompt_tokens,
                    "completion_tokens": completion.completion_tokens,
                    "facts": _facts_meta(facts, selection.lines, selection.omitted),
                }
                log.info("narrative.llm", kind=kind, model=completion.model, chars=len(completion.text))
                return Draft(kind, facts.at, completion.text, meta)
        text = render_template(facts, persona, reason=_REASON_TEXT.get(reason, reason))
        meta |= {
            "path": "template",
            "fallback_reason": reason,
            "error": error,
            "model": self.generator.model if self.generator else None,
            "facts": _facts_meta(facts, fact_lines(facts), {}),
        }
        log.info("narrative.template", kind=kind, reason=reason, error=error)
        return Draft(kind, facts.at, text, meta)

    async def report(
        self,
        kind: str,
        *,
        groups: list[EventGroup] | None = None,
        use_llm: bool = True,
        dry_run: bool = False,
        trigger: str = "scheduled",
    ) -> Report:
        """Compose and store a report (``dry_run``: compose only; the returned report has id 0)."""
        draft = await self.compose(kind, groups=groups, use_llm=use_llm, write_memory=not dry_run)
        draft.meta["trigger"] = trigger
        at = self.clock()
        if dry_run:
            return Report(id=0, kind=kind, generated_at=at, text=draft.text, meta=draft.meta)
        report = self.store.add_report(kind, at, draft.text, draft.meta)
        log.info("narrative.stored", kind=kind, report_id=report.id, path=draft.meta["path"])
        return report


def _facts_meta(facts: Facts, lines: list[Line], omitted: dict[str, int]) -> dict[str, Any]:
    refs: dict[str, list[str]] = {}
    for line in lines:
        if line.section != "now":
            refs.setdefault(line.section, []).append(line.ref)
    return {
        "events": refs.get("items", []),
        "proposed": refs.get("proposed", []),
        "approved": refs.get("approved", []),
        "rejections": refs.get("rejected", []),
        "notes": refs.get("notes", []),
        "tariff": facts.tariff.to_dict() if facts.tariff and "tariff" in refs else None,
        "omitted": omitted,
    }


def build_narrator(
    config: ScoutConfig,
    store: Store,
    budget: DailyBudget | None,
    *,
    tariff: TariffFile | None = None,
    clock: Clock = utc_now,
    generator: TextGenerator | None = None,
) -> Narrator:
    """The narrator ``config`` describes. With no ``services.llm`` every report uses the template."""
    llm = config.services.llm
    if generator is None and llm is not None:
        generator = TextGenerator(build_backend(llm), budget)
    return Narrator(
        store,
        generator=generator,
        persona=PersonaFile(config.files.persona),
        memory=Memory(config.files.memory),
        tariff=tariff or TariffFile(config.files.tariff),
        tz=config.schedule.tz,
        clock=clock,
        max_tokens=llm.max_tokens if llm else 400,
        context_tokens=llm.context_tokens if llm else 3072,
    )


__all__ = [
    "INSTRUCTIONS",
    "REPORT_KINDS",
    "SECTIONS",
    "Draft",
    "Facts",
    "Line",
    "Narrator",
    "Selection",
    "TariffFact",
    "build_narrator",
    "build_prompt",
    "fact_lines",
    "render_facts",
    "render_template",
    "select_lines",
]
