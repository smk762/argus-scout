"""The narrative: morning, evening and status reports written from structured facts.

Three steps, each its own function so it can be tested and explained:

1. **Facts** (:meth:`Narrator.gather`). Everything a report may say, read from scout's own state: open
   inbox items (grouped by fingerprint), recommendations awaiting a decision and approved ones, recent
   rejections with the reasons people gave, the tariff period and the next change, and the operator's
   plain-file notes (:mod:`argus_scout.memory`). Nothing else reaches the model, so nothing else can be
   claimed.
2. **Prompt** (:func:`build_prompt`). The facts become lines, each with a priority *tier* (critical items
   first, then warnings, the rejections, notices, info, and notes last). The prompt is sized for a small
   local model: ``context_tokens`` (default 3072) minus the output allowance (``max_tokens``, per kind by default:
   :data:`~argus_scout.config.DEFAULT_MAX_TOKENS`), the system
   prompt and the instructions, estimated at four characters a token, is what's left for facts. Lines are
   taken in priority order until the next one doesn't fit; every line not taken is counted, and the
   prompt says how many were left out per section. So the cut always falls on the least severe facts.
3. **Text.** The prompt goes to the model through :class:`~argus_scout.llm.TextGenerator` (charged to
   the ``llm_calls`` daily cap, under a GPU lease when ``services.llm.gpu`` is set). **If there is no model,
   it is over its cap, no GPU lease was granted in time, or it fails, the report is
   written from a deterministic template** (:func:`render_template`) from the same facts, so a briefing
   is never silently skipped. The report's ``meta.path`` says which ran (``llm`` or ``template``) and
   ``meta.fallback_reason`` why.
4. **Check** (:func:`assess_completion`). A model answer is used only if it is whole: not cut off by the token
   limit (``truncated``), and not hollow: past a low near-empty backstop (:data:`MIN_NARRATIVE_CHARS`), and naming every
   open item that is a warning, critical or needs an acknowledgement (:func:`must_mention`), and for a briefing
   every open recommendation at warning or above (:func:`must_mention_recs`), of those the prompt held
   (:func:`required`). Otherwise the
   template writes the report from the same facts, so a model failure never drops one of those items. The
   check is a floor, not a judgement of quality (:func:`mentions` says what counts as naming an item). A refused
   answer is kept for audit, never delivered: ``meta.rejected_text`` holds a cleaned, clipped copy and
   ``meta.rejected_check`` the rule that failed and the item ids it found missing (:func:`rejection_meta`). A
   report whose severity is ``warning`` or above also refuses an answer that calls things all clear
   (:data:`ALL_CLEAR_PHRASES`, rule ``contradicts_severity``).

**Grounding.** The facts open with the local time, its timezone and the report kind, and scout's own rules
(:data:`~argus_scout.persona.RULES`, first in the system prompt) say to greet for the report kind, never the
clock, and to lead with the most severe item and never call things nominal while warnings or criticals are open.
A persona can't override them: when the report is ``warning`` or above, persona lines (voice, style, do, sign-off)
that carry an all-clear phrase are withheld from the prompt and the template (:func:`ground_persona`).

Every report is saved to the store with ``meta`` recording the facts used (event, recommendation and
rejection ids, the tariff period, note names, what was left out), the persona and the model.

**Advisory only.** The model's text is stored and shown, never parsed. A report can describe and
recommend; nothing in it moves a recommendation or starts anything.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Awaitable, Callable, Collection
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal
from zoneinfo import ZoneInfo

import structlog

from argus_scout.budget import DailyBudget
from argus_scout.config import ScoutConfig, SuiteStatusSource, resolve_max_tokens
from argus_scout.engine import RecommendationEngine, Rejection
from argus_scout.events import StatusEvent
from argus_scout.gpu import narrative_guard
from argus_scout.inbox import EventGroup, Inbox
from argus_scout.llm import (
    CHARS_PER_TOKEN,
    LLM_CALLS,
    Completion,
    LLMUnavailable,
    Prompt,
    TextGenerator,
    build_backend,
    estimate_tokens,
)
from argus_scout.memory import Memory, MemoryPathError, Note, render_rejection
from argus_scout.persona import DEFAULT_PERSONA, Persona, PersonaFile
from argus_scout.recommendations import Recommendation, Status
from argus_scout.scheduler import Clock, utc_now
from argus_scout.severity import Severity, rank
from argus_scout.sources.base import SourceError
from argus_scout.store.base import Report, Store
from argus_scout.suite import SuiteStatus, render_report, spoken_summary, worst_severity
from argus_scout.tariff import TariffFile, current_period, format_rate, next_period_change

if TYPE_CHECKING:
    from argus_cortex.gpu import GpuCoordinator

    from argus_scout.prometheus import PrometheusClient

log = structlog.get_logger()

ReportKind = Literal["morning", "evening", "status"]
REPORT_KINDS: tuple[str, ...] = ("morning", "evening", "status")

SUITE_KIND = "suite"
"""The suite status summary: every Argus service with its health and version, read from Prometheus when it is
asked for. Written on request only (``POST /api/reports``, ``argus-scout brief --kind suite``), always as a plain
listing: no model writes it, so no version or state in it is ever a model's guess."""
ON_REQUEST_KINDS: tuple[str, ...] = (*REPORT_KINDS, SUITE_KIND)
"""The kinds a person may ask for. :data:`REPORT_KINDS` are the ones the model may write and the schedule runs."""
SUITE_TITLE = "Suite status"

SuiteReader = Callable[[], Awaitable[tuple[SuiteStatus, SuiteStatusSource]]]
"""Reads the suite from Prometheus now: the reading and the ``suite_status`` source settings it was read with."""


class SuiteNotConfigured(ValueError):
    """A suite report was asked for, but no ``suite_status`` source (with a Prometheus to ask) is configured."""


class SuiteUnavailable(Exception):
    """Prometheus could not answer, so the suite's state is unknown. No report is written: an "unknown" report
    stored or spoken beside real ones would be one more thing to misread as "nothing wrong"."""


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

GREETING: dict[str, str] = {
    "morning": 'If you greet, say "Good morning", whatever the time.',
    "evening": 'If you greet, say "Good evening", whatever the time.',
    "status": "No greeting.",
}
"""How each kind may greet. **A report greets for its kind, never for the clock**: a ``morning`` briefing written
at 23:00 by hand is still the morning briefing, so it says "Good morning" or nothing, never "Evening"."""

MIN_NARRATIVE_CHARS: dict[str, int] = {"morning": 60, "evening": 60, "status": 40}
"""A low backstop for a report with something in it: under this an answer is near-empty (the model ran out of
budget, or wrote a pleasantry instead of the report). It is deliberately low. Whether an answer covers what
matters is the ``missing_items`` rule's job, which checks the content directly: a terse answer that names every
warning, critical and needs-ack item is whole, however short (a real 156-character briefing naming three critical
items was once refused by a 160-character floor)."""
MIN_EMPTY_NARRATIVE_CHARS = 20
"""The shortest answer accepted when there is nothing to report: one short sentence saying so."""

# Priority tiers: lower is kept first when the prompt has to be cut.
_SEVERITY_TIER = {Severity.CRITICAL: 1, Severity.WARNING: 2, Severity.NOTICE: 4, Severity.INFO: 5}
_REJECTION_TIER = 3
_NOTE_TIER = 6


_SENTENCE_END = re.compile(r"[.!?](?=\s)")


def _clip(text: str, limit: int = MAX_LINE_CHARS) -> str:
    """``text`` on one line, at most ``limit`` characters. A longer one is cut at the last sentence end in the
    second half of the room, else at the last word boundary, and ends in ``…``: never mid-word, unless the room
    holds no word boundary at all."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    room = text[: limit - 1]
    if text[limit - 1] == " ":  # the cut falls on a word boundary already
        return room.rstrip() + "…"
    ends = [m.end() for m in _SENTENCE_END.finditer(room)]
    if ends and ends[-1] >= limit // 2:
        return room[: ends[-1]] + " …"
    space = room.rfind(" ")
    if space > 0:
        return room[:space].rstrip(" ,:;-–—(") + "…"
    return room.rstrip() + "…"


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
    minor_digits: int | None = None

    def line(self) -> str:
        now = f"{self.period} period now, {format_rate(self.rate, self.currency, self.unit, self.minor_digits)}"
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


def now_line(facts: Facts) -> str:
    """The first fact: local time with its timezone, and which report this is, so the model never guesses the time
    of day: ``Saturday 21 November 2026, 22:56 local time (Europe/London, GMT). This is the morning briefing.``"""
    key, abbrev = getattr(facts.at.tzinfo, "key", None), facts.at.strftime("%Z")
    zone = f"{key}, {abbrev}" if key and abbrev and key != abbrev else (key or abbrev or "UTC")
    title = TITLES.get(facts.kind, "report").lower()
    return f"{facts.at:%A %d %B %Y, %H:%M} local time ({zone}). This is the {title}."


def fact_lines(facts: Facts) -> list[Line]:
    """Every fact as a prioritised line, in display order within each section (most severe first)."""
    sections = KIND_SECTIONS.get(facts.kind, tuple(SECTIONS))
    lines: list[Line] = [Line("now", now_line(facts), 0)]
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
    persona, _ = ground_persona(persona, facts)
    system = persona.system_prompt()
    instructions = f"{INSTRUCTIONS.get(facts.kind, INSTRUCTIONS['status'])} {GREETING.get(facts.kind, '')}".strip()
    if facts.empty:
        instructions += " There are no open items or recommendations."
    severity = _worst_severity(facts)
    if rank(severity) >= rank(Severity.WARNING):
        instructions += (
            f" The facts include {severity.value} items: open with the most severe one. Do not call anything "
            "nominal, fine, normal, calm or quiet, and do not say there are no crises."
        )
    header = f"{instructions}\n\nFacts:\n"
    fixed = len(system) + len(header)
    budget = int((context_tokens - max_tokens) * CHARS_PER_TOKEN * PROMPT_MARGIN) - fixed
    selection = select_lines(fact_lines(facts), max(0, budget), notes_chars=notes_chars)
    return Prompt(system=system, user=header + render_facts(selection)), selection


# --- checking a model answer ----------------------------------------------------------------------------------

_TOKEN = re.compile(r"[a-z0-9]+")
_STOPWORDS_TEXT = """
the and for are was were not but has had have having its our out now new all any can did get got let may per via
off own who why how yet too was been being into onto from with without about above after again against also
before below between both check could does done down during each further here just less more most needs nothing
once only other over same should since some still such than that their them then there these they this those
through under until very what when where which while will would your you report reports reported times
acknowledgement acknowledge acknowledged info notice warning warnings critical status item items alert alerts
fine okay good well today tonight morning evening home lab homelab server servers system
systems service services host hosts monitor monitoring scout argus agent job jobs node nodes data app api bot
"""
_STOPWORDS_BASE = frozenset(_STOPWORDS_TEXT.split())
MIN_TOKEN_CHARS = 3
"""Shorter words are dropped from the words an item is looked for by (so a stray ``4`` or ``pr`` can't match);
a number of two digits or more is kept."""
STRONG_PAYLOAD_KEYS = ("host", "hostname", "instance", "node", "device", "mountpoint", "service", "job", "container")
"""Payload keys whose values name an item as strongly as its source does (and every value of a ``labels`` map)."""

_UNITS = [
    "zero",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
    "eleven",
    "twelve",
    "thirteen",
    "fourteen",
    "fifteen",
    "sixteen",
    "seventeen",
    "eighteen",
    "nineteen",
]
_TENS = ["twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]
_NUMBER_WORD = {w: i for i, w in enumerate(_UNITS)} | {w: 20 + 10 * i for i, w in enumerate(_TENS)}
_NUMBER_RUN = re.compile(
    rf"\b(?:{'|'.join(sorted(_NUMBER_WORD, key=len, reverse=True))}|hundred)"
    rf"(?:(?:[\s-]+|\s+and\s+)(?:{'|'.join(sorted(_NUMBER_WORD, key=len, reverse=True))}|hundred))*\b"
)


def _number_value(words: list[str]) -> int | None:
    """``["one", "hundred", "five"]`` -> 105: 0-999 in simple forms; ``None`` if the words don't make one number."""
    total, current, seen = 0, None, False
    for word in words:
        if word == "hundred":
            if current is None or current >= 10 or total:
                return None
            total, current = current * 100, None
            continue
        value = _NUMBER_WORD[word]
        if current is None:
            current = value
        elif current >= 20 and current % 10 == 0 and value < 10:
            current += value  # "twenty seven"
        else:
            return None
        seen = True
    if not seen and not total:
        return None
    return total + (current or 0)


def numbers_to_digits(text: str) -> str:
    """Spelled-out numbers (0-999: "twenty-seven", "one hundred and five") as digits, so the answer check matches
    them against the digits in an item. A run that isn't one number is left as it is."""

    def replace(match: re.Match[str]) -> str:
        words = [w for w in re.split(r"[\s-]+", match.group(0)) if w and w != "and"]
        value = _number_value(words)
        return match.group(0) if value is None else str(value)

    return _NUMBER_RUN.sub(replace, text.lower())


def _stem(word: str) -> str:
    """A crude stem, the same on both sides of a match: "decisions" ~ "decision", "rebooting" ~ "reboot",
    "batteries" ~ "battery". Not a dictionary: "restarting" stays "restart", so it never matches "rest"."""
    if word.isdigit():
        return word
    for suffix, repl in (("ies", "y"), ("ing", ""), ("ed", ""), ("es", ""), ("s", "")):
        if word.endswith(suffix) and not word.endswith("ss") and len(word) - len(suffix) + len(repl) >= MIN_TOKEN_CHARS:
            word = word[: -len(suffix)] + repl
            if suffix in ("ing", "ed") and len(word) > 3 and word[-1] == word[-2] and word[-1] not in "lsz":
                word = word[:-1]  # "stopped" -> "stop"
            break
    if len(word) > 4 and word.endswith("e"):
        word = word[:-1]  # "complete" ~ "completed" ~ "completes"
    return word


_STOPWORDS = frozenset(_stem(w) for w in _STOPWORDS_BASE)


def _tokens(text: str) -> list[str]:
    """Words of ``text``, split on anything that isn't a letter or digit (so ``ai-code-auditor`` is three)."""
    return _TOKEN.findall(numbers_to_digits(text))


def _keep(word: str) -> bool:
    if word.isdigit():
        return len(word) >= 2
    return len(word) >= MIN_TOKEN_CHARS


def _keywords(text: str) -> set[str]:
    """Distinctive stems of ``text``: three characters or more (or a number of two digits or more), no stopwords."""
    return {stem for w in _tokens(text) if _keep(w) and (stem := _stem(w)) not in _STOPWORDS}


def _strong_values(event: StatusEvent) -> list[str]:
    payload = event.payload or {}
    values = [payload.get(key) for key in STRONG_PAYLOAD_KEYS]
    labels = payload.get("labels")
    if isinstance(labels, dict):
        values += list(labels.values())
    return [event.source, *(v for v in values if isinstance(v, str))]


@dataclass(frozen=True)
class ItemWords:
    """What an answer has to contain to name an item (:func:`mentions`)."""

    strong: frozenset[str]
    """From the item's source (split on ``-``, ``_``, ``.``) and its host and label values: one is enough."""
    weak: frozenset[str]
    """From its summary: :attr:`needed` of them are."""

    @property
    def needed(self) -> int:
        return min(MIN_WEAK_HITS, len(self.weak))


MIN_WEAK_HITS = 2
"""Distinct summary words an answer must contain to name an item when it names none of its strong words."""


def item_words(group: EventGroup) -> ItemWords:
    event = group.latest
    strong = frozenset(k for value in _strong_values(event) for k in _keywords(value))
    return ItemWords(strong, frozenset(_keywords(event.summary) - strong))


def mentions(text: str, group: EventGroup) -> bool:
    """Whether ``text`` names the item. A floor, not proof. Both sides are split into words (on ``-``, ``_`` and
    punctuation), spelled-out numbers become digits, words under three characters and stopwords are dropped, and
    words match whole, by a crude stem ("decisions" ~ "decision", but "restarting" is not "rest"). An item is
    named by one of its strong words (its source's parts, host and label values) or by two distinct words of its
    summary (one, if the summary has only one). An item with no distinctive words at all is taken as named."""
    words = item_words(group)
    return names(text, words)


def names(text: str, words: ItemWords) -> bool:
    """Whether ``text`` names something described by ``words`` (the matching rules are :func:`mentions`')."""
    if not (words.strong or words.weak):
        return True
    said = {_stem(w) for w in _tokens(text)}
    if words.strong & said:
        return True
    return len(words.weak & said) >= max(1, words.needed)


def must_mention(facts: Facts) -> list[EventGroup]:
    """The open items no report may drop: warnings, criticals, and anything that needs an acknowledgement."""
    return [g for g in facts.groups if rank(g.latest.severity) >= rank(Severity.WARNING) or g.latest.requires_ack]


def must_mention_recs(facts: Facts) -> list[Recommendation]:
    """The open recommendations (proposed or approved) no briefing may drop: those at ``warning`` or above. A status
    update lists no recommendations (:data:`KIND_SECTIONS`), so it has none."""
    sections = KIND_SECTIONS.get(facts.kind, tuple(SECTIONS))
    recs = [*(facts.proposed if "proposed" in sections else []), *(facts.approved if "approved" in sections else [])]
    return [r for r in recs if rank(r.severity) >= rank(Severity.WARNING)]


_KEY_LABEL = re.compile(r'[A-Za-z_][A-Za-z0-9_]*="((?:[^"\\]|\\.)*)"')


def rec_words(rec: Recommendation) -> ItemWords:
    """What names a recommendation: **strong**, the label values in its dedup key (its rule's ``labels_key``, such
    as the host or instance); **weak**, the words of its title. Same matching as an item (:func:`mentions`)."""
    strong = frozenset(k for value in _KEY_LABEL.findall(rec.key) for k in _keywords(value))
    return ItemWords(strong, frozenset(_keywords(rec.title) - strong))


MAX_REQUIRED = 12
"""At most this many things an answer must name, most severe first: a briefing of 150 words can't name more."""


@dataclass(frozen=True)
class Required:
    """One thing an answer must name: an open item or a recommendation, by its fact-line ref."""

    ref: str
    severity: Severity
    words: ItemWords


def required(facts: Facts, shown: Collection[str] | None = None) -> list[Required]:
    """What an answer must name: every :func:`must_mention` item and :func:`must_mention_recs` recommendation,
    most severe first, capped at :data:`MAX_REQUIRED`. With ``shown`` (the refs of the fact lines that made it into
    the prompt), only those: the model is never held to a fact it wasn't given."""
    found = [Required(g.latest.event_id, g.latest.severity, item_words(g)) for g in must_mention(facts)]
    found += [Required(r.id, r.severity, rec_words(r)) for r in must_mention_recs(facts)]
    if shown is not None:
        found = [r for r in found if r.ref in shown]
    found.sort(key=lambda r: -rank(r.severity))  # stable: items before recommendations within a severity
    return found[:MAX_REQUIRED]


ALL_CLEAR_PHRASES: tuple[str, ...] = (
    r"all systems (?:are )?(?:nominal|normal|green|go)",
    r"all clear",
    r"all quiet",
    r"nothing to report",
    r"nothing (?:is )?exploding",
    r"nothing needs (?:your )?attention",
    r"no (?:immediate |urgent |major |real )?(?:crisis|crises)",
    r"everything(?: is|'s|’s) (?:fine|ok|okay|good|normal|nominal|calm|quiet)",
)
"""Phrases that say nothing is wrong. An answer for a report of ``warning`` or above that contains one is refused
(rule ``contradicts_severity``). Matched case-insensitively, as whole words. Kept tight on purpose: these are
absolute claims ("all clear", "everything is fine"), so qualified ones that follow the severe items ("nothing else
is urgent", "the rest is fine", "otherwise quiet") pass. A phrase in the list is refused wherever it appears,
even after the severe items ("host-a is critical; otherwise all clear"): the safe side is the template."""
_ALL_CLEAR = re.compile(r"\b(?:" + "|".join(ALL_CLEAR_PHRASES) + r")\b", re.IGNORECASE)


def all_clear_phrases(text: str) -> list[str]:
    """The all-clear phrases (:data:`ALL_CLEAR_PHRASES`) in ``text``, as written (whitespace runs as one space)."""
    return [m.group(0) for m in _ALL_CLEAR.finditer(" ".join(text.split()))]


def ground_persona(persona: Persona, facts: Facts) -> tuple[Persona, list[str]]:
    """``persona`` with every line that would contradict a report of ``warning`` or above withheld: a voice,
    style or do entry, or a sign-off, that carries an all-clear phrase (a catchphrase like "All systems nominal").
    Returns the persona to use and the withheld lines. Below ``warning`` the persona is returned as it is.

    Scout's rules come first in the system prompt and say they win over the persona; this makes sure a persona
    can't keep asking for the contradiction anyway, and keeps an all-clear sign-off off the template too."""
    if rank(_worst_severity(facts)) < rank(Severity.WARNING):
        return persona, []
    withheld: list[str] = []

    def keep(line: str) -> bool:
        if all_clear_phrases(line):
            withheld.append(line)
            return False
        return True

    update: dict[str, Any] = {
        "style": [s for s in persona.style if keep(s)],
        "do": [s for s in persona.do if keep(s)],
    }
    if not keep(persona.voice):
        update["voice"] = DEFAULT_PERSONA.voice
    if persona.sign_off and not keep(persona.sign_off):
        update["sign_off"] = ""
    if not withheld:
        return persona, []
    return persona.model_copy(update=update), withheld


@dataclass(frozen=True)
class Verdict:
    """Why a model answer was refused: :attr:`reason` is ``truncated``, ``hollow`` or ``contradicted``;
    :attr:`rule` the check that failed (``token_limit``, ``too_short``, ``missing_items`` or
    ``contradicts_severity``)."""

    reason: str
    rule: str
    detail: str
    missing: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {"reason": self.reason, "rule": self.rule, "detail": self.detail, "missing": list(self.missing)}


def check_completion(completion: Completion, facts: Facts, shown: Collection[str] | None = None) -> Verdict | None:
    """``None`` when the answer may be delivered; otherwise the :class:`Verdict`: ``truncated`` (the token limit
    cut it off), ``hollow`` (near-empty, or missing an item or recommendation :func:`required` names; ``shown`` is
    the refs of the fact lines the prompt held) or ``contradicted`` (an all-clear phrase in a report of ``warning``
    or above, :data:`ALL_CLEAR_PHRASES`)."""
    if completion.truncated:
        detail = f"the model hit its token limit ({completion.completion_tokens} tokens)"
        return Verdict("truncated", "token_limit", detail)
    text = completion.text
    floor = MIN_EMPTY_NARRATIVE_CHARS if facts.empty else MIN_NARRATIVE_CHARS.get(facts.kind, MIN_EMPTY_NARRATIVE_CHARS)
    if len(text) < floor:
        detail = f"the model's answer was {len(text)} characters, under {floor} for a {facts.kind} report"
        return Verdict("hollow", "too_short", detail)
    missing = tuple(r.ref for r in required(facts, shown) if not names(text, r.words))
    if missing:
        return Verdict("hollow", "missing_items", f"the model's answer left out {', '.join(missing)}", missing)
    severity = _worst_severity(facts)
    if rank(severity) >= rank(Severity.WARNING) and (found := all_clear_phrases(text)):
        quoted = ", ".join(f'"{p}"' for p in dict.fromkeys(f.lower() for f in found))
        detail = f"the model's answer called things all clear ({quoted}) in a {severity.value} report"
        return Verdict("contradicted", "contradicts_severity", detail)
    return None


def assess_completion(completion: Completion, facts: Facts) -> tuple[str, str] | None:
    """:func:`check_completion` as ``(reason, detail)``, or ``None`` when the answer may be delivered."""
    verdict = check_completion(completion, facts)
    return None if verdict is None else (verdict.reason, verdict.detail)


MAX_REJECTED_CHARS = 2000
"""How much of a refused model answer is kept in ``meta.rejected_text``."""
_KEEP_CONTROLS = {"\n", "\t"}


def clean_rejected(text: str, limit: int = MAX_REJECTED_CHARS) -> str:
    """A refused answer as it is safe to store and show: newlines normalised, control and format characters
    (terminal escapes, bidi overrides) stripped, clipped to ``limit`` characters with ``…``."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = "".join(c for c in text if c in _KEEP_CONTROLS or unicodedata.category(c) not in ("Cc", "Cf"))
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def rejection_meta(completion: Completion, verdict: Verdict) -> dict[str, Any]:
    """What a report's meta keeps about a refused answer, so the verdict can be audited: ``rejected_text`` (for
    the stored report and ``/api/reports``; stripped from every delivery, see
    :data:`~argus_scout.channels.base.UNDELIVERED_META`) and ``rejected_check``."""
    return {"rejected_text": clean_rejected(completion.text), "rejected_check": verdict.to_dict()}


# --- the template fallback -------------------------------------------------------------------------------------


def render_template(facts: Facts, persona: Persona, *, reason: str = "") -> str:
    """A plain-text report from the facts alone, no model: deterministic, and never empty. At most
    :data:`TEMPLATE_MAX_PER_SECTION` lines a section, except that every item :func:`must_mention` and every
    recommendation :func:`must_mention_recs` names is listed, uncapped."""
    lines = fact_lines(facts)
    keep = {g.latest.event_id for g in must_mention(facts)} | {r.id for r in must_mention_recs(facts)}
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
        # Past the cap, items that need attention are still listed: only the rest are counted.
        shown = [ln for i, ln in enumerate(section) if i < TEMPLATE_MAX_PER_SECTION or ln.ref in keep]
        out += [f"- {ln.text}" for ln in shown]
        if len(section) > len(shown):
            out.append(f"- and {len(section) - len(shown)} more")
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
    "gpu_unavailable": "no GPU was free for the model in time",
    "error": "the model was unavailable",
    "disabled": "the model was turned off for this run",
    "no_llm": "no model is configured",
    "truncated": "the model's answer was cut off",
    "hollow": "the model's answer left out what needs attention",
    "contradicted": "the model's answer called things all clear while items need attention",
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
        max_tokens: int | None = None,
        context_tokens: int = 3072,
        rejection_limit: int = 10,
        budget_kind: str = LLM_CALLS,
        suite: SuiteReader | None = None,
    ) -> None:
        self.store = store
        self.generator = generator
        self.persona = persona or PersonaFile(None)
        self.memory = memory or Memory(None)
        self.tariff = tariff or TariffFile(None)
        self.tz = tz or ZoneInfo("UTC")
        self.clock = clock
        self.max_tokens = max_tokens
        """One output allowance for every kind; ``None``: per kind (:func:`~argus_scout.config.resolve_max_tokens`)."""
        self.context_tokens = context_tokens
        self.rejection_limit = rejection_limit
        self.budget_kind = budget_kind
        self.suite = suite
        """Reads the suite for a ``suite`` report; ``None`` when no ``suite_status`` source is configured."""

    def max_tokens_for(self, kind: str) -> int:
        return resolve_max_tokens(kind, self.max_tokens, self.context_tokens)

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
            facts.tariff = TariffFact(
                period.name, period.rate, tariff.unit, tariff.currency, next_name, seconds, tariff.minor_digits
            )
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
        if kind == SUITE_KIND:
            return await self.compose_suite()
        if kind not in REPORT_KINDS:
            raise ValueError(f"unknown report kind {kind!r} (known: {', '.join(REPORT_KINDS)})")
        facts = self.gather(kind, groups=groups)
        if write_memory:
            self._sync_memory(facts.rejections)
        persona, withheld = ground_persona(self.persona.get(), facts)
        max_tokens = self.max_tokens_for(kind)
        prompt, selection = build_prompt(
            facts,
            persona,
            context_tokens=self.context_tokens,
            max_tokens=max_tokens,
            notes_chars=self.memory.max_chars,
        )
        meta: dict[str, Any] = {
            "persona": persona.name,
            "persona_error": self.persona.error,
            "persona_withheld": withheld,
            "prompt_tokens_est": estimate_tokens(prompt.system + prompt.user),
            "context_tokens": self.context_tokens,
            "max_tokens": max_tokens,
            "fingerprints": sorted(g.fingerprint for g in facts.groups),
            **delivery_meta(facts),
        }
        reason, error = "", None
        if not use_llm:
            reason = "disabled"
        elif self.generator is None:
            reason = "no_llm"
        else:
            try:
                completion = await self.generator.generate(prompt, max_tokens=max_tokens, kind=self.budget_kind)
            except LLMUnavailable as exc:
                reason, error = exc.reason, str(exc)
            else:
                meta |= {
                    "backend": completion.backend,
                    "prompt_tokens": completion.prompt_tokens,
                    "completion_tokens": completion.completion_tokens,
                    "finish_reason": completion.finish_reason,
                    "llm_chars": len(completion.text),
                }
                verdict = check_completion(completion, facts, {ln.ref for ln in selection.lines})
                if verdict is None:
                    meta |= {
                        "path": "llm",
                        "fallback_reason": None,
                        "model": completion.model,
                        "facts": _facts_meta(facts, selection.lines, selection.omitted),
                    }
                    log.info("narrative.llm", kind=kind, model=completion.model, chars=len(completion.text))
                    return Draft(kind, facts.at, completion.text, meta)
                reason, error = verdict.reason, verdict.detail
                meta |= rejection_meta(completion, verdict)
                log.warning(
                    "narrative.rejected_completion",
                    kind=kind,
                    reason=reason,
                    rule=verdict.rule,
                    missing=list(verdict.missing),
                    detail=error,
                    model=completion.model,
                    completion_tokens=completion.completion_tokens,
                    finish_reason=completion.finish_reason,
                )
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

    async def compose_suite(self) -> Draft:
        """The suite status summary, from one reading of Prometheus. No model, no persona, no memory: a listing of
        what Prometheus says. Raises :class:`SuiteNotConfigured` or :class:`SuiteUnavailable` instead of writing
        a report about nothing."""
        if self.suite is None:
            raise SuiteNotConfigured(
                "no suite_status source is configured (add one under sources:, and set services.prometheus_url)"
            )
        try:
            status, cfg = await self.suite()
        except SourceError as exc:
            raise SuiteUnavailable(str(exc)) from exc
        at = self.clock().astimezone(self.tz)
        counts = {state: len(status.in_state(state)) for state in ("ok", "up", "degraded", "starting", "down")}
        meta: dict[str, Any] = {
            "path": "template",
            "fallback_reason": "listing",
            "error": None,
            "model": None,
            "severity": worst_severity(status, cfg).value,
            "quiet_exempt": False,
            "title": SUITE_TITLE,
            "speakable": spoken_summary(status),
            "suite": {
                "source": cfg.name,
                "services": len(status.services),
                "states": counts,
                "out_of_date": len(status.behind),
                "version_skew": [skew.peer for skew in status.skews],
                "changed_version": len(status.changed),
            },
        }
        log.info("narrative.suite", services=len(status.services), **counts)
        return Draft(SUITE_KIND, at, render_report(status, cfg, local=at), meta)

    async def report(
        self,
        kind: str,
        *,
        groups: list[EventGroup] | None = None,
        use_llm: bool = True,
        dry_run: bool = False,
        trigger: str = "scheduled",
        meta: dict[str, Any] | None = None,
    ) -> Report:
        """Compose and store a report (``dry_run``: compose only; the returned report has id 0). ``meta`` is
        merged into the stored meta last (the status cycle passes the report's ``speakable`` this way)."""
        draft = await self.compose(kind, groups=groups, use_llm=use_llm, write_memory=not dry_run)
        draft.meta["trigger"] = trigger
        draft.meta |= meta or {}
        at = self.clock()
        if dry_run:
            return Report(id=0, kind=kind, generated_at=at, text=draft.text, meta=draft.meta)
        report = self.store.add_report(kind, at, draft.text, draft.meta)
        log.info("narrative.stored", kind=kind, report_id=report.id, path=draft.meta["path"])
        return report


def _worst_severity(facts: Facts) -> Severity:
    """The most severe thing a report is about: its items, and for a briefing its proposed and approved
    recommendations. ``info`` when there is nothing (rejections, the tariff and notes never count)."""
    found = [g.latest.severity for g in facts.groups]
    if facts.kind != "status":
        found += [r.severity for r in (*facts.proposed, *facts.approved)]
    return max(found, key=rank, default=Severity.INFO)


def delivery_meta(facts: Facts) -> dict[str, Any]:
    """What the channels read from a report's meta (:meth:`Delivery.from_report
    <argus_scout.channels.base.Delivery.from_report>`):

    - ``severity``: the worst item (:func:`_worst_severity`), so ``min_severity`` filters, text headers and
      webhook payloads match what the report is about. A briefing with nothing open is ``info``.
    - ``quiet_exempt``: true when any item in it is ``quiet_exempt``, so quiet hours treat the report the way
      the status cycle treated its items.
    - ``title``: the report's title; a status report's names its worst item.
    """
    severity = _worst_severity(facts)
    title = TITLES.get(facts.kind, "Report")
    if facts.kind == "status" and facts.groups:
        worst = max(facts.groups, key=lambda g: (rank(g.latest.severity), g.latest.generated_at)).latest
        more = len(facts.groups) - 1
        title = f"{title}: {_clip(worst.summary, 120)}" + (f" (and {more} more)" if more else "")
    return {
        "severity": severity.value,
        "quiet_exempt": any(g.latest.quiet_exempt for g in facts.groups),
        "title": title,
    }


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
    coordinator: GpuCoordinator | None = None,
    prometheus: PrometheusClient | None = None,
) -> Narrator:
    """The narrator ``config`` describes. With no ``services.llm`` every report uses the template.

    With ``services.llm.gpu`` each model call holds a GPU lease from ``coordinator`` (default: the host-wide
    ``ARGUS_GPU_*`` environment, see :mod:`argus_scout.gpu`)."""
    llm = config.services.llm
    memory = Memory(config.files.memory, write_rejections=config.memory.write_rejections)
    memory.check_writable()  # once, at startup: an unwritable directory is one warning, not one per report
    if generator is None and llm is not None:
        generator = TextGenerator(build_backend(llm), budget, call_guard=narrative_guard(llm, coordinator))
    return Narrator(
        store,
        generator=generator,
        persona=PersonaFile(config.files.persona),
        memory=memory,
        tariff=tariff or TariffFile(config.files.tariff),
        tz=config.schedule.tz,
        clock=clock,
        max_tokens=llm.max_tokens if llm else None,
        context_tokens=llm.context_tokens if llm else 3072,
        suite=suite_reader(config, clock=clock, prometheus=prometheus),
    )


def suite_reader(
    config: ScoutConfig, *, clock: Clock = utc_now, prometheus: PrometheusClient | None = None
) -> SuiteReader | None:
    """How a ``suite`` report reads the suite: the first ``suite_status`` source's settings, against its own
    ``prometheus_url`` or ``services.prometheus_url``. ``None`` with no such source or no Prometheus to ask.
    ``prometheus`` replaces the client (tests)."""
    from argus_scout.sources.suite_status import read_once

    entry = next((s for s in config.sources if isinstance(s, SuiteStatusSource)), None)
    url = entry and (entry.prometheus_url or config.services.prometheus_url)
    if entry is None or not (url or prometheus):
        return None
    cfg = entry.model_copy(update={"prometheus_url": url})

    async def read() -> tuple[SuiteStatus, SuiteStatusSource]:
        return await read_once(cfg, client=prometheus, now=clock()), cfg

    return read


__all__ = [
    "ALL_CLEAR_PHRASES",
    "GREETING",
    "INSTRUCTIONS",
    "MIN_EMPTY_NARRATIVE_CHARS",
    "MIN_NARRATIVE_CHARS",
    "ON_REQUEST_KINDS",
    "REPORT_KINDS",
    "SUITE_KIND",
    "SuiteNotConfigured",
    "SuiteUnavailable",
    "SECTIONS",
    "Draft",
    "Facts",
    "Line",
    "Narrator",
    "Selection",
    "TariffFact",
    "Verdict",
    "all_clear_phrases",
    "assess_completion",
    "check_completion",
    "clean_rejected",
    "build_narrator",
    "build_prompt",
    "delivery_meta",
    "fact_lines",
    "ground_persona",
    "mentions",
    "MAX_REQUIRED",
    "Required",
    "must_mention",
    "must_mention_recs",
    "names",
    "rec_words",
    "required",
    "now_line",
    "numbers_to_digits",
    "rejection_meta",
    "render_facts",
    "render_template",
    "select_lines",
]
