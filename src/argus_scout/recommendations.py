"""Recommendation lifecycle.

    draft → proposed → approved → deployed → retired
               │    ↘ rejected (with reason)
               └──→ retired (expired: the signal cleared, or it went stale unanswered)

Scout only ever moves a recommendation as far as ``approved``. ``deployed``
is a person recording that they carried it out; nothing in scout executes a
recommendation. ``proposed → retired`` withdraws a proposal nobody acted on;
retiring is terminal and does nothing, so it keeps the advisory-only rule.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import uuid4

from argus_scout.events import parse_dt
from argus_scout.severity import Severity


class Status(StrEnum):
    DRAFT = "draft"
    PROPOSED = "proposed"
    APPROVED = "approved"
    REJECTED = "rejected"
    DEPLOYED = "deployed"
    RETIRED = "retired"


class Confidence(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


TRANSITIONS: dict[Status, frozenset[Status]] = {
    Status.DRAFT: frozenset({Status.PROPOSED}),
    Status.PROPOSED: frozenset({Status.APPROVED, Status.REJECTED, Status.RETIRED}),
    Status.APPROVED: frozenset({Status.DEPLOYED, Status.RETIRED}),
    Status.DEPLOYED: frozenset({Status.RETIRED}),
    Status.REJECTED: frozenset(),
    Status.RETIRED: frozenset(),
}

ApprovalSource = Literal["manual", "standing"]


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass
class Recommendation:
    """One proposal from an analyzer."""

    id: str = field(default_factory=lambda: str(uuid4()))
    title: str = ""
    action: str = ""
    rationale: str = ""
    confidence: Confidence = Confidence.MEDIUM
    impact: str = ""
    status: Status = Status.DRAFT
    analyzer: str = ""
    evidence: list[str] = field(default_factory=list)
    # Highest severity of the signals behind this proposal.
    severity: Severity = Severity.INFO
    created_at: datetime = field(default_factory=_now)
    updated_at: datetime = field(default_factory=_now)
    rejection_reason: str = ""
    # Approval audit trail: who approved it, and under which standing rule.
    approval_source: ApprovalSource | None = None
    approved_by_rule: str | None = None
    approved_at: datetime | None = None
    # Dedup identity: the signal rule plus the label values that make an instance distinct
    # (see :func:`argus_scout.signals.dedup_key`). Empty for a recommendation not raised from a signal.
    key: str = ""
    # When an unanswered proposal goes stale and is retired; refreshed while its signal holds.
    expires_at: datetime | None = None
    # When the signal behind it was last seen holding.
    last_seen_at: datetime | None = None
    retired_reason: str = ""
    # The store's write counter for this row, bumped on every save (0 until first saved). A person's decision names
    # the revision they viewed, and the store refuses it if the row has changed since.
    revision: int = 0

    def transition(self, new_status: Status, *, now: datetime | None = None) -> bool:
        """Move to ``new_status`` if the lifecycle allows it. Returns whether it moved."""
        if new_status not in TRANSITIONS[self.status]:
            return False
        self.status = new_status
        self.updated_at = now or _now()
        return True

    def approve(self, *, rule_id: str | None = None, now: datetime | None = None) -> bool:
        """Approve a proposal; ``rule_id`` marks a standing approval. Never deploys."""
        if not self.transition(Status.APPROVED, now=now):
            return False
        self.approval_source = "standing" if rule_id else "manual"
        self.approved_by_rule = rule_id
        self.approved_at = self.updated_at
        return True

    def reject(self, reason: str = "", *, now: datetime | None = None) -> bool:
        if not self.transition(Status.REJECTED, now=now):
            return False
        self.rejection_reason = reason
        return True

    def retire(self, reason: str = "", *, now: datetime | None = None) -> bool:
        """Withdraw: the proposal no longer applies. Does nothing beyond recording that."""
        if not self.transition(Status.RETIRED, now=now):
            return False
        self.retired_reason = reason
        return True

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Recommendation:
        """The inverse of :meth:`to_dict`; naive datetimes are read as UTC."""
        approval_source = raw.get("approval_source")
        return cls(
            id=str(raw["id"]),
            title=str(raw.get("title") or ""),
            action=str(raw.get("action") or ""),
            rationale=str(raw.get("rationale") or ""),
            confidence=Confidence(raw.get("confidence") or Confidence.MEDIUM),
            impact=str(raw.get("impact") or ""),
            status=Status(raw.get("status") or Status.DRAFT),
            analyzer=str(raw.get("analyzer") or ""),
            evidence=[str(e) for e in raw.get("evidence") or []],
            severity=Severity(raw.get("severity") or Severity.INFO),
            created_at=parse_dt(raw.get("created_at")) or _now(),
            updated_at=parse_dt(raw.get("updated_at")) or _now(),
            rejection_reason=str(raw.get("rejection_reason") or ""),
            approval_source=approval_source if approval_source in ("manual", "standing") else None,
            approved_by_rule=raw.get("approved_by_rule") or None,
            approved_at=parse_dt(raw.get("approved_at")),
            key=str(raw.get("key") or ""),
            expires_at=parse_dt(raw.get("expires_at")),
            last_seen_at=parse_dt(raw.get("last_seen_at")),
            retired_reason=str(raw.get("retired_reason") or ""),
            revision=int(raw.get("revision") or 0),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "action": self.action,
            "rationale": self.rationale,
            "confidence": self.confidence.value,
            "impact": self.impact,
            "status": self.status.value,
            "analyzer": self.analyzer,
            "evidence": list(self.evidence),
            "severity": self.severity.value,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "rejection_reason": self.rejection_reason,
            "approval_source": self.approval_source,
            "approved_by_rule": self.approved_by_rule,
            "approved_at": self.approved_at.isoformat() if self.approved_at else None,
            "key": self.key,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "last_seen_at": self.last_seen_at.isoformat() if self.last_seen_at else None,
            "retired_reason": self.retired_reason,
            "revision": self.revision,
        }
