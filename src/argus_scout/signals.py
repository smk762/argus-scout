"""Declarative signal rules: a PromQL query, a threshold, how long it must hold, and what to propose.

Rules live in a YAML file (``files.signals`` in ``scout.yaml``; see ``examples/signals.yaml``) and
replace hard-coded analyzers: anything "query Prometheus, compare each series with a threshold, propose
something about the ones that cross it" is a rule, not code. ::

    rules:
      - name: disk-filling
        query: 100 * (1 - node_filesystem_avail_bytes / node_filesystem_size_bytes)
        op: ">"
        threshold: 85
        for: 15m
        severity: warning
        escalate:                       # optional: a further threshold raises the severity
          - {threshold: 95, severity: critical}
        labels_key: [instance, mountpoint]
        rejection_ttl: 30d              # optional: a rejection is forgotten after this long
        expires_after: 7d               # optional: an unanswered proposal goes stale after this long
        recommendation:
          title: "{mountpoint} on {instance} is {value:.0f}% full"
          action: Free space on {mountpoint} or grow the volume.
          rationale: Above {threshold:.0f}% for {for}.
          impact: low
          confidence: medium

**Evaluation.** Each sample of the query's instant vector is compared with ``op threshold``. A sample
that crosses it is *pending* until the same instance (its ``labels_key`` values) has crossed it on every
cycle for ``for``; then it is a :class:`SignalHit`. Pending start times are kept in a
:class:`SignalState`, which persists them through the store, so a restart doesn't reset the clock.

**Unknown is not all clear.** If Prometheus can't answer a rule's query, the rule is *unknown* for that
cycle: its pending state is kept as it was, it yields no hits, and :class:`Evaluation` records it, so the
engine knows not to treat its silence as the condition clearing.

**Templates.** The recommendation text is rendered by :func:`render`, a small ``str.format``-alike that
never calls ``format_map`` on caller data: placeholders must be plain names from an allowlist (the
reserved names below, the rule's ``labels_key``, and any extra ``labels`` it lists), there is no attribute
or index access, no ``!r``/``!s`` conversion, and the only format spec is a precision on a number
(``{value:.1f}``, ``{value:.0%}``). A template that breaks those rules makes its rule invalid at load
time. A label missing from a sample renders as ``?``.

Reserved placeholders: ``value``, ``threshold``, ``op``, ``rule``, ``severity``, ``for``.

**Bad rules degrade.** An invalid rule is skipped and reported; the others still load. The file is
re-read when it changes (:class:`SignalRulesFile`), and a file that won't parse at all gives no rules
plus an error, never a crash. ``argus-scout check signals FILE`` validates it.

**Other evaluators.** The engine only sees :class:`SignalHit` and :class:`Evaluation`, so anything
that can produce them (tariff-aware rules, for one) plugs into the same dedup, expiry, approval and
rejection memory: build hits with :func:`make_hit` and combine evaluations with :meth:`Evaluation.merge`.
"""

from __future__ import annotations

import math
import operator
import re
import string
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Protocol

import structlog
import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from argus_scout.prometheus import PrometheusClient, PrometheusError, Sample
from argus_scout.recommendations import Confidence
from argus_scout.severity import Severity, rank

log = structlog.get_logger()

RESERVED = frozenset({"value", "threshold", "op", "rule", "severity", "for"})
NUMERIC = frozenset({"value", "threshold"})
MISSING_LABEL = "?"
MAX_LABEL_CHARS = 200
MAX_TEMPLATE_CHARS = 2000

_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_RULE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")
_SPEC = re.compile(r"^\.[0-9][f%]$")
_DURATION = re.compile(r"^(?:(\d+)w)?(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?$")
_UNITS = ("weeks", "days", "hours", "minutes", "seconds")

Op = Literal[">", ">=", "<", "<=", "==", "!="]
OPS: dict[str, Callable[[float, float], bool]] = {
    ">": operator.gt,
    ">=": operator.ge,
    "<": operator.lt,
    "<=": operator.le,
    "==": operator.eq,
    "!=": operator.ne,
}


# --- durations ------------------------------------------------------------------------------------------------


def parse_duration(value: Any) -> timedelta:
    """``90`` (seconds), ``"30s"``, ``"15m"``, ``"1h30m"``, ``"7d"``, ``"2w"`` -> timedelta."""
    if isinstance(value, timedelta):
        return value
    if isinstance(value, bool):
        raise ValueError(f"{value!r} is not a duration")
    if isinstance(value, int | float):
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"{value!r} is not a duration")
        return timedelta(seconds=value)
    text = str(value).strip()
    match = _DURATION.match(text)
    if not text or not match:
        raise ValueError(f"{value!r} is not a duration (like 30s, 15m, 1h30m, 7d)")
    return timedelta(**{unit: int(n) for unit, n in zip(_UNITS, match.groups(), strict=True) if n})


def format_duration(value: timedelta) -> str:
    """The inverse of :func:`parse_duration`, compactly: ``timedelta(minutes=90)`` -> ``1h30m``."""
    seconds = int(value.total_seconds())
    if seconds == 0:
        return "0s"
    parts = []
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
        n, seconds = divmod(seconds, size)
        if n:
            parts.append(f"{n}{unit}")
    return "".join(parts)


# --- templates ------------------------------------------------------------------------------------------------


def template_fields(template: str, allowed: Iterable[str]) -> list[str]:
    """The placeholders in ``template``; raises ValueError on anything :func:`render` won't do."""
    if len(template) > MAX_TEMPLATE_CHARS:
        raise ValueError(f"longer than {MAX_TEMPLATE_CHARS} characters")
    allowed = set(allowed)
    names = []
    try:
        parsed = list(string.Formatter().parse(template))
    except ValueError as exc:
        raise ValueError(f"bad template: {exc}") from exc
    for _, name, spec, conversion in parsed:
        if name is None:
            continue
        if not name:
            raise ValueError("positional placeholders ({}) are not allowed; name a label")
        if not _NAME.match(name):
            raise ValueError(f"{{{name}}}: only plain names, no attribute or index access")
        if conversion is not None:
            raise ValueError(f"{{{name}!{conversion}}}: conversions are not allowed")
        if spec:
            if "{" in spec:
                raise ValueError(f"{{{name}}}: nested placeholders are not allowed")
            if name not in NUMERIC or not _SPEC.match(spec):
                raise ValueError(f"{{{name}:{spec}}}: only a precision on value/threshold, like :.1f or :.0%")
        if name not in allowed:
            raise ValueError(f"{{{name}}} is not a reserved name ({', '.join(sorted(RESERVED))}) or a listed label")
        names.append(name)
    return names


def _number(value: float, spec: str) -> str:
    if not math.isfinite(value):
        return str(value)
    if spec:
        return format(value, spec)
    return f"{value:g}"


def render(template: str, values: Mapping[str, Any], allowed: Iterable[str]) -> str:
    """Fill ``template`` from ``values``. Validates it first (see :func:`template_fields`).

    Values are inserted as text, never formatted or evaluated themselves; only ``value`` and
    ``threshold`` take a (precision-only) format spec. A missing name renders as ``?``.
    """
    template_fields(template, allowed)
    out = []
    for literal, name, spec, _ in string.Formatter().parse(template):
        out.append(literal)
        if name is None:
            continue
        value = values.get(name)
        if value is None:
            out.append(MISSING_LABEL)
        elif name in NUMERIC and isinstance(value, int | float):
            out.append(_number(float(value), spec or ""))
        else:
            out.append(str(value)[:MAX_LABEL_CHARS])
    return "".join(out)


# --- rules ----------------------------------------------------------------------------------------------------


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class RecommendationTemplate(_Strict):
    title: str
    action: str
    rationale: str = ""
    impact: str = "low"
    confidence: Confidence = Confidence.MEDIUM


class Escalation(_Strict):
    threshold: float
    severity: Severity


class SignalRule(_Strict):
    """One rule. See the module docstring for the YAML shape."""

    name: str
    query: str
    op: Op = ">"
    threshold: float
    for_: timedelta = Field(default=timedelta(0), alias="for")
    severity: Severity = Severity.WARNING
    escalate: list[Escalation] = Field(default_factory=list)
    labels_key: list[str] = Field(default_factory=lambda: ["instance"])
    labels: list[str] = Field(default_factory=list)
    """Extra labels the templates may quote (beyond ``labels_key``); they don't affect identity."""
    rejection_ttl: timedelta | None = None
    expires_after: timedelta | None = None
    recommendation: RecommendationTemplate

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        if not _RULE_NAME.match(value):
            raise ValueError("letters, digits, '.', '_' and '-' only (up to 100), starting with a letter or digit")
        return value

    @field_validator("query")
    @classmethod
    def _query(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be empty")
        return value.strip()

    @field_validator("threshold")
    @classmethod
    def _finite(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("must be a finite number")
        return value

    @field_validator("for_", "rejection_ttl", "expires_after", mode="before")
    @classmethod
    def _duration(cls, value: Any) -> Any:
        return None if value is None else parse_duration(value)

    @field_validator("labels_key", "labels")
    @classmethod
    def _label_names(cls, value: list[str]) -> list[str]:
        for name in value:
            if not _NAME.match(name):
                raise ValueError(f"{name!r} is not a Prometheus label name")
        if len(set(value)) != len(value):
            raise ValueError("repeats a label")
        return value

    @model_validator(mode="after")
    def _check(self) -> SignalRule:
        previous = self.threshold
        for step in self.escalate:
            if self.op in ("==", "!="):
                raise ValueError("escalate needs an ordering op (>, >=, <, <=)")
            past = step.threshold > previous if self.op.startswith(">") else step.threshold < previous
            if not past:
                raise ValueError(f"escalate threshold {step.threshold:g} is not past {previous:g} for op {self.op}")
            if rank(step.severity) <= rank(self.severity):
                raise ValueError(f"escalate severity {step.severity} is not above {self.severity}")
            previous = step.threshold
        allowed = self.placeholders
        for part in ("title", "action", "rationale", "impact"):
            try:
                template_fields(getattr(self.recommendation, part), allowed)
            except ValueError as exc:
                raise ValueError(f"recommendation.{part}: {exc}") from exc
        return self

    @property
    def placeholders(self) -> frozenset[str]:
        return RESERVED | set(self.labels_key) | set(self.labels)

    def matches(self, value: float) -> bool:
        """Whether ``value`` crosses the threshold (NaN never does)."""
        return not math.isnan(value) and OPS[self.op](value, self.threshold)

    def severity_for(self, value: float) -> Severity:
        severity = self.severity
        for step in self.escalate:
            if OPS[self.op](value, step.threshold):
                severity = step.severity
        return severity


def dedup_key(rule: str, labels: Mapping[str, str], labels_key: Iterable[str]) -> str:
    """``rule{label="value",...}``: stable, readable, and the same for the same instance every cycle."""

    def quote(text: str) -> str:
        return text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")

    inner = ",".join(f'{name}="{quote(labels.get(name, ""))}"' for name in labels_key)
    return f"{rule}{{{inner}}}"


@dataclass
class RuleSet:
    """What a rules file loaded to: the good rules, and why any were skipped."""

    rules: list[SignalRule] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    file_error: str | None = None


def _describe(exc: ValidationError) -> str:
    parts = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err["loc"] if p != "for_") or "rule"
        msg = err["msg"].removeprefix("Value error, ")
        parts.append(f"{loc}: {'unknown key' if err['type'] == 'extra_forbidden' else msg}")
    return "; ".join(parts)


def parse_rules(data: Any) -> RuleSet:
    """Rules from parsed YAML (``{rules: [...]}``). A bad rule is skipped with an error; the rest load."""
    result = RuleSet()
    if data is None:
        return result
    if not isinstance(data, dict):
        result.file_error = "expected a mapping with a 'rules' list"
        return result
    unknown = sorted(set(data) - {"rules"})
    if unknown:
        result.errors.append(f"unknown top-level keys ignored: {', '.join(map(str, unknown))}")
    raw_rules = data.get("rules") or []
    if not isinstance(raw_rules, list):
        result.file_error = "'rules' must be a list"
        return result
    seen: set[str] = set()
    for i, raw in enumerate(raw_rules):
        label = f"rule {i}"
        if isinstance(raw, dict) and raw.get("name"):
            label = f"rule {i} ({raw['name']})"
        if not isinstance(raw, dict):
            result.errors.append(f"{label}: expected a mapping")
            continue
        try:
            rule = SignalRule.model_validate(raw)
        except ValidationError as exc:
            result.errors.append(f"{label}: {_describe(exc)}")
            continue
        if rule.name in seen:
            result.errors.append(f"{label}: duplicate name; the first one is kept")
            continue
        seen.add(rule.name)
        result.rules.append(rule)
    return result


def load_signal_rules(path: str | Path) -> RuleSet:
    """Rules from a YAML file. Never raises for bad content: it lands in ``file_error`` / ``errors``."""
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError, UnicodeDecodeError) as exc:
        return RuleSet(file_error=str(exc))
    return parse_rules(data)


class SignalRulesFile:
    """Signal rules backed by a YAML file, re-read when its mtime changes (as :class:`PolicyFile` does).

    A missing file gives no rules. A bad edit gives the rules that still parse; a file that fails to
    parse at all gives none, with ``file_error`` set. Neither stops scout.
    """

    def __init__(self, path: str | Path | None) -> None:
        self.path = str(path) if path else ""
        self._mtime: float | None = None
        self._cached = RuleSet()

    def get(self) -> RuleSet:
        if not self.path:
            return RuleSet()
        try:
            mtime = Path(self.path).stat().st_mtime
        except OSError:
            self._mtime, self._cached = None, RuleSet()
            return self._cached
        if mtime == self._mtime:
            return self._cached
        ruleset = load_signal_rules(self.path)
        if ruleset.file_error:
            log.warning("signals.load_failed", path=self.path, error=ruleset.file_error)
        for error in ruleset.errors:
            log.warning("signals.bad_rule", path=self.path, error=error)
        log.info("signals.rules_loaded", path=self.path, count=len(ruleset.rules))
        self._mtime, self._cached = mtime, ruleset
        return ruleset


# --- pending state --------------------------------------------------------------------------------------------


class PendingStore(Protocol):
    """Where :class:`SignalState` persists (see :class:`argus_scout.store.Store`)."""

    def load_signal_pending(self) -> dict[str, dict[str, datetime]]: ...

    def save_signal_pending(self, rule: str, pending: dict[str, datetime]) -> None: ...

    def drop_signal_pending(self, keep: Iterable[str]) -> int: ...


class SignalState:
    """Since when each rule instance has crossed its threshold. With a ``store`` it survives restarts."""

    def __init__(self, store: PendingStore | None = None) -> None:
        self.store = store
        self._pending: dict[str, dict[str, datetime]] = store.load_signal_pending() if store is not None else {}

    def pending(self, rule: str) -> dict[str, datetime]:
        return dict(self._pending.get(rule, {}))

    def update(self, rule: str, pending: dict[str, datetime]) -> None:
        if pending == self._pending.get(rule, {}):
            return
        if pending:
            self._pending[rule] = dict(pending)
        else:
            self._pending.pop(rule, None)
        if self.store is not None:
            self.store.save_signal_pending(rule, pending)

    def keep_only(self, rules: Iterable[str]) -> None:
        keep = set(rules)
        dropped = [name for name in self._pending if name not in keep]
        for name in dropped:
            del self._pending[name]
        if dropped and self.store is not None:
            self.store.drop_signal_pending(keep)


# --- evaluation -----------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class SignalHit:
    """A rule instance that has held for its ``for`` duration: the input to the engine."""

    rule: str
    key: str
    labels: Mapping[str, str]
    value: float
    threshold: float
    severity: Severity
    since: datetime
    title: str
    action: str
    rationale: str = ""
    impact: str = "low"
    confidence: Confidence = Confidence.MEDIUM
    rejection_ttl: timedelta | None = None
    expires_after: timedelta | None = None

    @property
    def evidence(self) -> str:
        value, threshold = _number(self.value, ""), _number(self.threshold, "")
        return f"{self.key} = {value} (threshold {threshold}) since {self.since.astimezone(UTC):%Y-%m-%d %H:%M}Z"


def make_hit(rule: SignalRule, sample: Sample, since: datetime) -> SignalHit:
    """Render ``rule``'s recommendation for one sample."""
    values: dict[str, Any] = {name: sample.labels.get(name) for name in set(rule.labels_key) | set(rule.labels)}
    severity = rule.severity_for(sample.value)
    values.update(
        value=sample.value,
        threshold=rule.threshold,
        op=rule.op,
        rule=rule.name,
        severity=severity.value,
        **{"for": format_duration(rule.for_)},
    )
    allowed = rule.placeholders
    tpl = rule.recommendation
    return SignalHit(
        rule=rule.name,
        key=dedup_key(rule.name, sample.labels, rule.labels_key),
        labels=dict(sample.labels),
        value=sample.value,
        threshold=rule.threshold,
        severity=severity,
        since=since,
        title=render(tpl.title, values, allowed),
        action=render(tpl.action, values, allowed),
        rationale=render(tpl.rationale, values, allowed),
        impact=render(tpl.impact, values, allowed),
        confidence=tpl.confidence,
        rejection_ttl=rule.rejection_ttl,
        expires_after=rule.expires_after,
    )


class Evaluation(list[SignalHit]):
    """The hits of one evaluation (it *is* the list), plus which rules ran and which were unknown.

    ``evaluated`` names every rule whose query was answered, so an instance of it that isn't in the
    list has cleared. ``unknown`` maps each rule that couldn't be evaluated to why; the engine treats
    those as "can't tell", never as cleared.
    """

    def __init__(
        self,
        hits: Iterable[SignalHit] = (),
        *,
        evaluated: Iterable[str] = (),
        unknown: Mapping[str, str] | None = None,
        pending: int = 0,
    ) -> None:
        super().__init__(hits)
        self.evaluated: set[str] = set(evaluated)
        self.unknown: dict[str, str] = dict(unknown or {})
        self.pending = pending

    def merge(self, other: Evaluation) -> Evaluation:
        return Evaluation(
            [*self, *other],
            evaluated=self.evaluated | other.evaluated,
            unknown={**self.unknown, **other.unknown},
            pending=self.pending + other.pending,
        )


async def evaluate(
    rules: Iterable[SignalRule], client: PrometheusClient, now: datetime, *, state: SignalState | None = None
) -> Evaluation:
    """Run every rule's query once and return the instances that have held for their ``for``.

    Without a ``state`` the pending clock starts fresh each call, so only ``for: 0`` rules can fire.
    """
    state = state if state is not None else SignalState()
    rules = list(rules)
    result = Evaluation()
    for rule in rules:
        try:
            samples = await client.query(rule.query, at=now)
        except PrometheusError as exc:
            result.unknown[rule.name] = str(exc)
            log.warning("signals.rule_unknown", rule=rule.name, error=str(exc))
            continue
        result.evaluated.add(rule.name)
        # Two series sharing the identity labels are one instance: keep the worse of them.
        crossing: dict[str, Sample] = {}
        for sample in samples:
            if not rule.matches(sample.value):
                continue
            key = dedup_key(rule.name, sample.labels, rule.labels_key)
            prev = crossing.get(key)
            if prev is None or rank(rule.severity_for(sample.value)) > rank(rule.severity_for(prev.value)):
                crossing[key] = sample
        before = state.pending(rule.name)
        pending: dict[str, datetime] = {}
        for key, sample in crossing.items():
            since = before.get(key) or now
            pending[key] = since
            if now - since >= rule.for_:
                result.append(make_hit(rule, sample, since))
            else:
                result.pending += 1
        state.update(rule.name, pending)
    state.keep_only(r.name for r in rules)
    return result


__all__ = [
    "Evaluation",
    "PendingStore",
    "RuleSet",
    "SignalHit",
    "SignalRule",
    "SignalRulesFile",
    "SignalState",
    "dedup_key",
    "evaluate",
    "format_duration",
    "load_signal_rules",
    "make_hit",
    "parse_duration",
    "parse_rules",
    "render",
    "template_fields",
]
