from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from argus_scout.approvals import StandingApprovals, briefing_lines, impact_level
from argus_scout.recommendations import Confidence, Recommendation, Status
from argus_scout.severity import Severity

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "approvals.yaml"

RULE = """
rules:
  - id: evening-shift
    scope: analyzer
    analyzer: power
    title_pattern: "(defer|shift).*off-?peak"
"""

PERMISSIVE = "rules:\n  - id: anything\n    analyzer: '*'\n    max_impact: medium\n    min_confidence: low\n"


def rec(
    title="Defer NAS scrub to off-peak",
    *,
    analyzer="power",
    impact="low",
    conf="medium",
    severity="warning",
    action="Move the scrub to 02:00",
) -> Recommendation:
    return Recommendation(
        title=title,
        action=action,
        impact=f"{impact} savings",
        confidence=Confidence(conf),
        analyzer=analyzer,
        severity=Severity(severity),
        status=Status.PROPOSED,
    )


@pytest.fixture
def standing(write_file):
    def make(text: str = RULE) -> StandingApprovals:
        return StandingApprovals(write_file("approvals.yaml", text))

    return make


def test_shipped_example_has_no_active_rules():
    assert StandingApprovals(EXAMPLE).rules() == []


def test_file_rule_auto_approves_and_audits(standing):
    approvals = standing()
    r = rec()
    rule = approvals.apply(r)
    assert rule is not None and rule.rule_id == "evening-shift"
    assert r.status == Status.APPROVED  # approved, never deployed
    assert r.approval_source == "standing"
    assert r.approved_by_rule == "evening-shift"
    assert r.approved_at is not None
    assert approvals.match_counts == {"evening-shift": 1}


@pytest.mark.parametrize(
    "kwargs",
    [
        {"analyzer": "thermal"},
        {"title": "Replace the UPS battery"},
        {"impact": "medium"},  # rule default max_impact: low
        {"conf": "low"},  # rule default min_confidence: medium
    ],
)
def test_non_matching_left_proposed(standing, kwargs):
    r = rec(**kwargs)
    assert standing().apply(r) is None
    assert r.status == Status.PROPOSED


@pytest.mark.parametrize("kwargs", [{"impact": "high"}, {"severity": "critical"}])
def test_guardrails_override_permissive_rules(standing, kwargs):
    r = rec(**kwargs)
    assert standing(PERMISSIVE).apply(r) is None
    assert r.status == Status.PROPOSED


def test_only_proposed_recommendations_are_touched(standing):
    r = rec()
    r.status = Status.DRAFT
    assert standing(PERMISSIVE).apply(r) is None
    assert r.status == Status.DRAFT


def test_until_scope_expires(standing):
    past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    approvals = standing(f"rules:\n  - id: old\n    scope: until\n    analyzer: power\n    expires_at: '{past}'\n")
    assert approvals.apply(rec()) is None


def test_bad_rules_skipped_others_kept(standing):
    approvals = standing(
        "rules:\n  - id: broken\n    scope: until\n"  # no expiry
        "  - id: badregex\n    title_pattern: '('\n"
        "  - id: ok\n    analyzer: power\n"
    )
    assert [r.rule_id for r in approvals.rules()] == ["ok"]


def test_file_reloads_on_change(standing, write_file):
    approvals = standing()
    assert [r.rule_id for r in approvals.rules()] == ["evening-shift"]
    write_file("approvals.yaml", "rules: []\n")
    assert approvals.rules() == []


def test_runtime_rule_matches_same_title_only():
    approvals = StandingApprovals()
    first = rec()
    assert first.approve()
    rule = approvals.add_from(first, "analyzer")
    assert rule is not None and rule.origin == "api"

    again, other = rec("defer NAS scrub to OFF-PEAK!"), rec("Defer Plex transcodes to off-peak")
    assert approvals.apply(again).rule_id == rule.rule_id
    assert approvals.apply(other) is None
    assert approvals.remove(rule.rule_id)
    assert approvals.rules() == []


def test_runtime_rule_refused_for_guarded_proposal():
    with pytest.raises(ValueError, match="high impact"):
        StandingApprovals().add_from(rec(impact="high"), "analyzer")
    # Approving it once is still the person's call.
    risky = rec(impact="high")
    assert risky.approve()
    assert risky.approval_source == "manual"


def test_runtime_until_needs_expiry():
    approvals = StandingApprovals()
    with pytest.raises(ValueError, match="expiry"):
        approvals.add_from(rec(), "until")
    assert approvals.add_from(rec(), "once") is None
    rule = approvals.add_from(rec(), "until", expires_at=datetime.now(UTC) + timedelta(hours=2))
    assert rule is not None and not rule.expired()


def test_file_rules_cannot_be_removed_at_runtime(standing):
    approvals = standing()
    assert not approvals.remove("evening-shift")
    assert approvals.get("evening-shift") is not None


def test_briefing_line(standing):
    approvals = standing()
    recs = [rec(), rec("Shift backups to off-peak")]
    for r in recs:
        approvals.apply(r)
    lines = briefing_lines(recs)
    assert len(lines) == 1
    assert "2 recommendation(s) auto-approved" in lines[0]
    assert "rule evening-shift" in lines[0]
    assert "nothing was deployed" in lines[0]


def test_lifecycle_rejects_invalid_transitions():
    r = rec()
    assert r.reject("not now")
    assert not r.approve()
    assert r.status == Status.REJECTED
    assert r.rejection_reason == "not now"


def test_impact_levels():
    assert impact_level(Recommendation(impact="High savings, ~20/mo")) == "high"
    assert impact_level(Recommendation(impact="minor")) == "low"
    assert impact_level(Recommendation(impact="3/month")) == "medium"


SRC = Path(__file__).resolve().parent.parent / "src" / "argus_scout"


def test_no_code_path_sets_deployed():
    """Only a person records ``deployed``. Outside the lifecycle's own definition (recommendations.py),
    nothing in src/ may name ``Status.DEPLOYED`` or the "deployed" status string."""
    offenders = []
    for path in SRC.rglob("*.py"):
        rel = path.relative_to(SRC).as_posix()
        if rel == "recommendations.py":
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Attribute) and node.attr == "DEPLOYED":
                offenders.append(f"{rel}:{node.lineno}")
            if isinstance(node, ast.Constant) and node.value == "deployed":
                offenders.append(f"{rel}:{node.lineno}")
    assert offenders == []
