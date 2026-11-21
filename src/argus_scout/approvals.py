"""Standing approvals: scoped auto-approve for recurring low-risk recommendations.

A rule matches a class of recommendation and approves it when it reaches
``proposed``. Scopes:

- ``once``: approve just this recommendation; no rule is kept.
- ``analyzer``: every matching proposal from this analyzer, until the rule is removed.
- ``until``: matching proposals until ``expires_at``.

Rules come from a YAML file (read-only, reloaded when it changes; see
``examples/approvals.yaml``) and from :meth:`StandingApprovals.add_from`.

Guardrails, which no rule can override:

- A standing approval only moves a recommendation to ``approved``. It never
  deploys or dispatches anything; carrying it out stays a human step.
- Proposals with high impact, or raised from a critical signal, are never
  auto-approved.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Protocol
from uuid import uuid4

import structlog
import yaml

from argus_scout.events import parse_dt
from argus_scout.recommendations import Recommendation, Status
from argus_scout.severity import Severity

log = structlog.get_logger()

Scope = Literal["once", "analyzer", "until"]
_LEVELS = {"low": 0, "medium": 1, "high": 2}


def normalise_title(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()


def impact_level(rec: Recommendation) -> str:
    """Coarse impact level from the free-text ``impact`` field (unknown → medium)."""
    text = rec.impact.lower()
    if re.search(r"\b(high|critical|major|significant|severe)\b", text):
        return "high"
    if re.search(r"\b(low|minor|small|negligible|marginal)\b", text):
        return "low"
    return "medium"


def guardrail_block(rec: Recommendation) -> str | None:
    """Why ``rec`` may never be auto-approved, or None."""
    if rec.severity == Severity.CRITICAL:
        return "critical signal"
    if impact_level(rec) == "high":
        return "high impact"
    return None


def _level(value: Any, default: str, allowed: tuple[str, ...]) -> str:
    text = str(value or "").strip().lower()
    return text if text in allowed else default


@dataclass
class StandingRule:
    rule_id: str
    scope: Scope = "analyzer"
    analyzer: str = "*"
    title_pattern: str | None = None  # regex (case-insensitive), searched in title
    title_exact: str | None = None  # normalised title equality
    action_pattern: str | None = None  # regex (case-insensitive), searched in action
    max_impact: str = "low"  # low | medium (high is always refused)
    min_confidence: str = "medium"  # low | medium | high
    expires_at: datetime | None = None
    origin: Literal["file", "api"] = "file"
    note: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def expired(self, now: datetime | None = None) -> bool:
        return self.expires_at is not None and (now or datetime.now(UTC)) >= self.expires_at

    def matches(self, rec: Recommendation, now: datetime | None = None) -> bool:
        if self.scope == "once" or self.expired(now):
            return False
        if self.scope == "until" and self.expires_at is None:
            return False
        if self.analyzer not in ("*", rec.analyzer):
            return False
        if self.title_exact and normalise_title(rec.title) != normalise_title(self.title_exact):
            return False
        if self.title_pattern and not re.search(self.title_pattern, rec.title, re.IGNORECASE):
            return False
        if self.action_pattern and not re.search(self.action_pattern, rec.action, re.IGNORECASE):
            return False
        if _LEVELS[impact_level(rec)] > _LEVELS.get(self.max_impact, 0):
            return False
        return _LEVELS[rec.confidence.value] >= _LEVELS.get(self.min_confidence, 1)

    @classmethod
    def from_dict(cls, raw: dict[str, Any], index: int = 0) -> StandingRule:
        scope = str(raw.get("scope") or "analyzer").lower()
        if scope not in ("analyzer", "until"):
            raise ValueError(f"scope must be analyzer or until, got {scope!r}")
        for key in ("title_pattern", "action_pattern"):
            if raw.get(key):
                re.compile(str(raw[key]))
        rule = cls(
            rule_id=str(raw.get("id") or f"file-{index}"),
            scope=scope,  # type: ignore[arg-type]
            analyzer=str(raw.get("analyzer") or "*"),
            title_pattern=str(raw["title_pattern"]) if raw.get("title_pattern") else None,
            action_pattern=str(raw["action_pattern"]) if raw.get("action_pattern") else None,
            max_impact=_level(raw.get("max_impact"), "low", ("low", "medium")),
            min_confidence=_level(raw.get("min_confidence"), "medium", ("low", "medium", "high")),
            expires_at=parse_dt(raw.get("expires_at")),
            origin="file",
            note=str(raw.get("note") or ""),
        )
        if rule.scope == "until" and rule.expires_at is None:
            raise ValueError("scope 'until' needs expires_at")
        return rule

    @classmethod
    def from_record(cls, raw: dict[str, Any]) -> StandingRule:
        """The inverse of :meth:`to_dict`, for rules read back from a store (any origin)."""
        origin = raw.get("origin")
        return cls(
            rule_id=str(raw["rule_id"]),
            scope=raw.get("scope") or "analyzer",
            analyzer=str(raw.get("analyzer") or "*"),
            title_pattern=raw.get("title_pattern") or None,
            title_exact=raw.get("title_exact") or None,
            action_pattern=raw.get("action_pattern") or None,
            max_impact=_level(raw.get("max_impact"), "low", ("low", "medium")),
            min_confidence=_level(raw.get("min_confidence"), "medium", ("low", "medium", "high")),
            expires_at=parse_dt(raw.get("expires_at")),
            origin=origin if origin in ("file", "api") else "api",
            note=str(raw.get("note") or ""),
            created_at=parse_dt(raw.get("created_at")) or datetime.now(UTC),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "scope": self.scope,
            "analyzer": self.analyzer,
            "title_pattern": self.title_pattern,
            "title_exact": self.title_exact,
            "action_pattern": self.action_pattern,
            "max_impact": self.max_impact,
            "min_confidence": self.min_confidence,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "expired": self.expired(),
            "origin": self.origin,
            "note": self.note,
            "created_at": self.created_at.isoformat(),
        }


def load_rules(path: str | Path) -> tuple[list[StandingRule], list[str]]:
    """Rules from a YAML file, and an error per rule that was skipped."""
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    raw_rules = data.get("rules") if isinstance(data, dict) else None
    rules: list[StandingRule] = []
    errors: list[str] = []
    for i, raw in enumerate(raw_rules or []):
        try:
            rules.append(StandingRule.from_dict(raw if isinstance(raw, dict) else {}, i))
        except Exception as exc:  # noqa: BLE001 — skip a bad rule, keep the rest
            errors.append(f"rule {i}: {exc}")
    return rules, errors


class RuntimeRuleStore(Protocol):
    """Where runtime rules persist (see :class:`argus_scout.store.Store`)."""

    def load_runtime_rules(self) -> list[StandingRule]: ...

    def save_runtime_rule(self, rule: StandingRule) -> None: ...

    def delete_runtime_rule(self, rule_id: str) -> bool: ...


class StandingApprovals:
    """File rules (reloaded on change) plus rules added at runtime.

    With a ``store``, runtime rules are loaded from it on construction and
    written through on :meth:`add_from` / :meth:`remove`, so they survive a
    restart. File rules always come from the YAML file and are never stored.
    """

    def __init__(self, path: str | Path | None = None, *, store: RuntimeRuleStore | None = None) -> None:
        self.path = str(path) if path else ""
        self._file_rules: dict[str, StandingRule] = {}
        self._file_mtime: float | None = None
        self._api_rules: dict[str, StandingRule] = {}
        self.match_counts: dict[str, int] = {}
        self.store = store
        if store is not None:
            self._api_rules = {r.rule_id: r for r in store.load_runtime_rules()}

    def _reload(self) -> None:
        if not self.path:
            return
        try:
            mtime = Path(self.path).stat().st_mtime
        except OSError:
            self._file_rules, self._file_mtime = {}, None
            return
        if mtime == self._file_mtime:
            return
        self._file_mtime = mtime
        try:
            rules, errors = load_rules(self.path)
        except Exception as exc:  # noqa: BLE001 — keep running with no file rules
            log.warning("standing_approval.load_failed", path=self.path, error=str(exc))
            rules, errors = [], []
        for error in errors:
            log.warning("standing_approval.bad_rule", path=self.path, error=error)
        self._file_rules = {r.rule_id: r for r in rules}
        log.info("standing_approval.rules_loaded", path=self.path, count=len(rules))

    def rules(self, *, include_expired: bool = True) -> list[StandingRule]:
        self._reload()
        rules = [*self._file_rules.values(), *self._api_rules.values()]
        return rules if include_expired else [r for r in rules if not r.expired()]

    def get(self, rule_id: str) -> StandingRule | None:
        return next((r for r in self.rules() if r.rule_id == rule_id), None)

    def remove(self, rule_id: str) -> bool:
        """Remove a runtime rule. File rules are removed by editing the file."""
        removed = self._api_rules.pop(rule_id, None) is not None
        if removed and self.store is not None:
            self.store.delete_runtime_rule(rule_id)
        return removed

    def add_from(
        self,
        rec: Recommendation,
        scope: Scope,
        *,
        expires_at: datetime | None = None,
        title_pattern: str | None = None,
        max_impact: str | None = None,
    ) -> StandingRule | None:
        """A runtime rule covering proposals like ``rec``. ``once`` creates nothing.

        Refuses to create a rule for a proposal the guardrails would block.
        """
        if scope == "once":
            return None
        blocked = guardrail_block(rec)
        if blocked:
            raise ValueError(f"no standing approval for this proposal: {blocked}")
        if scope == "until" and expires_at is None:
            raise ValueError("scope 'until' needs an expiry")
        if title_pattern:
            re.compile(title_pattern)
        rule = StandingRule(
            rule_id=f"api-{uuid4().hex[:8]}",
            scope=scope,
            analyzer=rec.analyzer or "*",
            title_pattern=title_pattern,
            title_exact=None if title_pattern else rec.title,
            max_impact=_level(max_impact, impact_level(rec), ("low", "medium")),
            min_confidence=rec.confidence.value,
            expires_at=expires_at,
            origin="api",
            note=f"from recommendation {rec.id}",
        )
        if self.store is not None:
            self.store.save_runtime_rule(rule)
        self._api_rules[rule.rule_id] = rule
        log.info("standing_approval.rule_added", **rule.to_dict())
        return rule

    def find_match(self, rec: Recommendation, now: datetime | None = None) -> StandingRule | None:
        if rec.status != Status.PROPOSED:
            return None
        match = next((r for r in self.rules() if r.matches(rec, now)), None)
        blocked = guardrail_block(rec)
        if blocked and match:
            log.info("standing_approval.guardrail", rec_id=rec.id, title=rec.title, reason=blocked)
            return None
        return match

    def apply(self, rec: Recommendation) -> StandingRule | None:
        """Approve ``rec`` if a standing rule covers it. Never deploys."""
        rule = self.find_match(rec)
        if rule is None or not rec.approve(rule_id=rule.rule_id):
            return None
        self.match_counts[rule.rule_id] = self.match_counts.get(rule.rule_id, 0) + 1
        return rule


def auto_approved_since(recs: Iterable[Recommendation], since: datetime) -> dict[str, int]:
    """Recommendations auto-approved per rule since ``since``."""
    counts: dict[str, int] = {}
    for rec in recs:
        if rec.approved_by_rule and rec.approved_at and rec.approved_at >= since:
            counts[rec.approved_by_rule] = counts.get(rec.approved_by_rule, 0) + 1
    return counts


def briefing_lines(recs: Iterable[Recommendation], hours: int = 24, now: datetime | None = None) -> list[str]:
    """A briefing line that keeps standing approvals visible to the person."""
    now = now or datetime.now(UTC)
    counts = auto_approved_since(recs, now - timedelta(hours=hours))
    if not counts:
        return []
    total = sum(counts.values())
    per_rule = ", ".join(f"{n} under rule {rule_id}" for rule_id, n in counts.items())
    return [
        f"Standing approvals: {total} recommendation(s) auto-approved in the last {hours}h "
        f"({per_rule}). Approved only; nothing was deployed."
    ]


__all__ = [
    "RuntimeRuleStore",
    "Scope",
    "StandingApprovals",
    "StandingRule",
    "auto_approved_since",
    "briefing_lines",
    "guardrail_block",
    "impact_level",
    "load_rules",
    "normalise_title",
]
