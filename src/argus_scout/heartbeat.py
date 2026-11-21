"""Heartbeat policy: one place that answers "is this worth interrupting for?".

A policy is built from defaults overridden by whichever keys a YAML file (or
dict) sets. :class:`PolicyFile` re-reads the file when it changes, so edits
apply on the next status tick. Every gate returns the rule that decided it,
so "why did / didn't scout say that?" always has an answer.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, time
from pathlib import Path
from typing import Any, Literal

import structlog
import yaml

from argus_scout.events import StatusEvent
from argus_scout.severity import Severity, rank

log = structlog.get_logger()

SourceChannel = Literal["voice", "text", "briefing"]
CHANNELS: tuple[str, ...] = ("voice", "text", "briefing")
ORIGIN_KEYS = (
    "report.min_severity",
    "report.max_items",
    "voice.min_severity",
    "voice.on_change_only",
    "voice.backoff_max_seconds",
    "rest_hours",
    "quiet_hours",
    "quiet_hours.min_severity",
)


_SEVERITY_NAMES = frozenset(s.value for s in Severity)


def _sev(value: Any, default: str) -> str:
    text = str(value or "").strip().lower()
    return text if text in _SEVERITY_NAMES else default


@dataclass(frozen=True)
class Window:
    """Local wall-clock window; may cross midnight. An empty start or end disables it."""

    start: str = ""
    end: str = ""

    def __post_init__(self) -> None:
        for value in (self.start, self.end):
            if value:
                time.fromisoformat(value)  # fail at load, not at the first tick

    @property
    def enabled(self) -> bool:
        return bool(self.start and self.end)

    def contains(self, now: datetime | None = None) -> bool:
        if not self.enabled:
            return False
        now = now or datetime.now().astimezone()
        start, end = time.fromisoformat(self.start), time.fromisoformat(self.end)
        t = now.time()
        if start <= end:
            return start <= t < end
        return t >= start or t < end

    def to_dict(self) -> dict[str, str]:
        return {"start": self.start, "end": self.end}


@dataclass(frozen=True)
class SourceRule:
    """Per-source routing.

    - ``voice``: may be spoken (subject to the voice rules)
    - ``text``: included in status updates, never spoken
    - ``briefing``: never interrupts; only appears in scheduled briefings
    """

    channel: SourceChannel = "voice"
    min_severity: str | None = None  # overrides report.min_severity for this source

    def to_dict(self) -> dict[str, Any]:
        return {"channel": self.channel, "min_severity": self.min_severity}


@dataclass
class HeartbeatPolicy:
    report_min_severity: str = Severity.NOTICE.value
    report_max_items: int = 4
    voice_min_severity: str = Severity.WARNING.value
    voice_on_change_only: bool = True
    voice_backoff_max_seconds: int = 21600
    rest_hours: Window = field(default_factory=Window)
    quiet_hours: Window = field(default_factory=Window)
    quiet_min_severity: str = Severity.CRITICAL.value
    # severity → seconds: re-announce an unchanged, still-unacked item after this long
    reannounce_after: dict[str, int] = field(default_factory=dict)
    sources: dict[str, SourceRule] = field(default_factory=dict)
    path: str = ""
    file_loaded: bool = False
    file_error: str | None = None
    # dotted key → "default" | "file"
    origin: dict[str, str] = field(default_factory=lambda: dict.fromkeys(ORIGIN_KEYS, "default"))

    def source_rule(self, source: str) -> tuple[SourceRule, str]:
        """Rule for ``source`` and the key that matched (exact, ``*`` or default)."""
        if source in self.sources:
            return self.sources[source], f"sources.{source}"
        if "*" in self.sources:
            return self.sources["*"], "sources.*"
        return SourceRule(), "sources.default"

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "file_loaded": self.file_loaded,
            "file_error": self.file_error,
            "report": {
                "min_severity": self.report_min_severity,
                "max_items": self.report_max_items,
                "always_include": ["requires_ack", "quiet_exempt"],
            },
            "voice": {
                "min_severity": self.voice_min_severity,
                "on_change_only": self.voice_on_change_only,
                "backoff_max_seconds": self.voice_backoff_max_seconds,
            },
            "rest_hours": self.rest_hours.to_dict(),
            "quiet_hours": {
                **self.quiet_hours.to_dict(),
                "min_severity": self.quiet_min_severity,
                "exempt": ["critical", "quiet_exempt"],
            },
            "reannounce_after": dict(self.reannounce_after),
            "sources": {k: v.to_dict() for k, v in self.sources.items()},
            "origin": dict(self.origin),
        }


def apply_overrides(base: HeartbeatPolicy, data: dict[str, Any]) -> HeartbeatPolicy:
    """A copy of ``base`` with every key present in ``data`` applied."""
    policy = replace(
        base,
        origin=dict(base.origin),
        reannounce_after=dict(base.reannounce_after),
        sources=dict(base.sources),
    )

    def section(name: str) -> dict[str, Any]:
        val = data.get(name)
        return val if isinstance(val, dict) else {}

    report, voice = section("report"), section("voice")
    rest, quiet = section("rest_hours"), section("quiet_hours")

    def mark(key: str) -> None:
        policy.origin[key] = "file"

    if "min_severity" in report:
        policy.report_min_severity = _sev(report["min_severity"], policy.report_min_severity)
        mark("report.min_severity")
    if "max_items" in report:
        policy.report_max_items = max(0, int(report["max_items"]))
        mark("report.max_items")
    if "min_severity" in voice:
        policy.voice_min_severity = _sev(voice["min_severity"], policy.voice_min_severity)
        mark("voice.min_severity")
    if "on_change_only" in voice:
        policy.voice_on_change_only = bool(voice["on_change_only"])
        mark("voice.on_change_only")
    if "backoff_max_seconds" in voice:
        policy.voice_backoff_max_seconds = max(0, int(voice["backoff_max_seconds"]))
        mark("voice.backoff_max_seconds")
    if "start" in rest or "end" in rest:
        policy.rest_hours = Window(str(rest.get("start") or ""), str(rest.get("end") or ""))
        mark("rest_hours")
    if "start" in quiet or "end" in quiet:
        policy.quiet_hours = Window(str(quiet.get("start") or ""), str(quiet.get("end") or ""))
        mark("quiet_hours")
    if "min_severity" in quiet:
        policy.quiet_min_severity = _sev(quiet["min_severity"], policy.quiet_min_severity)
        mark("quiet_hours.min_severity")

    for sev, seconds in section("reannounce").items():
        name = _sev(sev, "")
        if name and seconds and int(seconds) > 0:
            policy.reannounce_after[name] = int(seconds)

    for source, raw in section("sources").items():
        raw = raw if isinstance(raw, dict) else {}
        channel = str(raw.get("channel") or "voice").lower()
        if channel not in CHANNELS:
            log.warning("heartbeat.policy_bad_channel", source=source, channel=channel)
            channel = "voice"
        min_sev = _sev(raw.get("min_severity"), "") or None
        policy.sources[str(source)] = SourceRule(channel=channel, min_severity=min_sev)  # type: ignore[arg-type]
    return policy


class PolicyFile:
    """A heartbeat policy backed by a YAML file, re-read when its mtime changes.

    A missing file gives ``defaults``. A file that fails to parse also gives
    ``defaults`` (with ``file_error`` set): a bad edit must not stop status updates.
    """

    def __init__(self, path: str | Path | None, defaults: HeartbeatPolicy | None = None) -> None:
        self.path = str(path) if path else ""
        self.defaults = defaults or HeartbeatPolicy()
        self._mtime: float | None = None
        self._cached: HeartbeatPolicy | None = None

    def get(self) -> HeartbeatPolicy:
        base = replace(self.defaults, path=self.path)
        if not self.path:
            return base
        p = Path(self.path)
        try:
            mtime = p.stat().st_mtime
        except OSError:
            return base
        if self._cached is not None and mtime == self._mtime:
            return self._cached
        try:
            loaded = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            if not isinstance(loaded, dict):
                raise ValueError("heartbeat policy must be a YAML mapping")
            policy = apply_overrides(base, loaded)
            policy.file_loaded = True
        except Exception as exc:  # noqa: BLE001 — fall back, report the error
            log.warning("heartbeat.policy_load_failed", path=self.path, error=str(exc))
            policy = replace(base, file_error=str(exc))
        self._mtime, self._cached = mtime, policy
        return policy


@dataclass(frozen=True)
class Decision:
    """Why an event was or wasn't included / voiced."""

    event_id: str
    source: str
    severity: str
    broadcast: bool  # passes ack / expiry / quiet-hours gate
    report: bool  # included in a scheduled status update
    voice: bool  # eligible to be spoken (before change / backoff / budget checks)
    rule: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "source": self.source,
            "severity": self.severity,
            "broadcast": self.broadcast,
            "report": self.report,
            "voice": self.voice,
            "rule": self.rule,
        }


def broadcast_gate(event: StatusEvent, policy: HeartbeatPolicy, *, now: datetime, quiet: bool) -> tuple[bool, str]:
    """Ack / expiry / quiet-hours gate. ``quiet`` is whether quiet hours are active."""
    if event.expired(now):
        return False, "expired"
    if event.acked:
        return False, "acked"
    if quiet:
        if event.quiet_exempt:
            return True, "quiet_hours.exempt:quiet_exempt"
        if rank(event.severity) >= rank(Severity.CRITICAL):
            return True, "quiet_hours.exempt:critical"
        rule = f"quiet_hours.min_severity={policy.quiet_min_severity}"
        return rank(event.severity) >= rank(policy.quiet_min_severity), rule
    return True, "open"


def report_gate(event: StatusEvent, policy: HeartbeatPolicy) -> tuple[bool, str]:
    """Whether a broadcastable event belongs in a scheduled status update."""
    rule, key = policy.source_rule(event.source)
    if rule.channel == "briefing":
        return False, f"{key}.channel=briefing"
    min_sev = rule.min_severity or policy.report_min_severity
    where = f"{key}.min_severity" if rule.min_severity else "report.min_severity"
    if rank(event.severity) >= rank(min_sev):
        return True, f"{where}={min_sev}"
    if event.requires_ack:
        return True, "report.always_include:requires_ack"
    if event.quiet_exempt:
        return True, "report.always_include:quiet_exempt"
    return False, f"{where}={min_sev}"


def voice_gate(event: StatusEvent, policy: HeartbeatPolicy) -> tuple[bool, str]:
    rule, key = policy.source_rule(event.source)
    if rule.channel != "voice":
        return False, f"{key}.channel={rule.channel}"
    if not event.speakable:
        return False, "event.speakable=false"
    ok = rank(event.severity) >= rank(policy.voice_min_severity)
    return ok, f"voice.min_severity={policy.voice_min_severity}"


def explain(event: StatusEvent, policy: HeartbeatPolicy, *, now: datetime, quiet: bool) -> Decision:
    sev = event.severity.value
    ok, rule = broadcast_gate(event, policy, now=now, quiet=quiet)
    if not ok:
        return Decision(event.event_id, event.source, sev, False, False, False, rule)
    report, report_rule = report_gate(event, policy)
    if not report:
        return Decision(event.event_id, event.source, sev, True, False, False, report_rule)
    voice, voice_rule = voice_gate(event, policy)
    return Decision(event.event_id, event.source, sev, True, True, voice, f"{report_rule}; {voice_rule}")


def reannounce_due(group: list[StatusEvent], policy: HeartbeatPolicy, now: datetime | None = None) -> bool:
    """An unchanged item is news again once its severity's re-announce interval passes."""
    if not group:
        return False
    interval = policy.reannounce_after.get(group[0].severity.value)
    stamps = [e.last_broadcast_at for e in group if e.last_broadcast_at]
    if not interval or not stamps:
        return False
    now = now or datetime.now(UTC)
    return (now - max(stamps)).total_seconds() >= interval
