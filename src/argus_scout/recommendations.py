"""Recommendation lifecycle.

    draft → proposed → approved → deployed → retired
                    ↘ rejected (with reason)

Scout only ever moves a recommendation as far as ``approved``. ``deployed``
is a person recording that they carried it out; nothing in scout executes a
recommendation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import uuid4

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
    Status.PROPOSED: frozenset({Status.APPROVED, Status.REJECTED}),
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

    def transition(self, new_status: Status) -> bool:
        """Move to ``new_status`` if the lifecycle allows it. Returns whether it moved."""
        if new_status not in TRANSITIONS[self.status]:
            return False
        self.status = new_status
        self.updated_at = _now()
        return True

    def approve(self, *, rule_id: str | None = None) -> bool:
        """Approve a proposal; ``rule_id`` marks a standing approval. Never deploys."""
        if not self.transition(Status.APPROVED):
            return False
        self.approval_source = "standing" if rule_id else "manual"
        self.approved_by_rule = rule_id
        self.approved_at = self.updated_at
        return True

    def reject(self, reason: str = "") -> bool:
        if not self.transition(Status.REJECTED):
            return False
        self.rejection_reason = reason
        return True

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
        }
