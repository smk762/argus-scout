"""A model answer that states what the facts don't (a number no fact holds, or "critical" when nothing is) is refused
as ``ungrounded`` and the report comes from the template. The refused answers here are real ones, from a 1B model in
the demo."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from test_grounding import completion
from test_narrative import FakeGenerator, narrator

from argus_scout.narrative import (
    Facts,
    TariffFact,
    check_completion,
    numbers_in,
    overstated_severity,
    unsupported_numbers,
)
from argus_scout.recommendations import Recommendation, Status
from argus_scout.severity import Severity

AT = datetime(2026, 11, 28, 9, 59, tzinfo=ZoneInfo("UTC"))


def demo_facts() -> Facts:
    """What the demo's evening review held: a critical GPU recommendation and a notice tariff shift, both approved."""
    gpu = Recommendation(
        id="g1", title="GPU 0 on demo-gpu-node is running hot (92°C)", severity=Severity.CRITICAL,
        action="Check the GPU's fans and airflow, or cap its power limit.",
        rationale="The GPU has been at or above 90°C for 1m.", status=Status.APPROVED,
    )  # fmt: skip
    shift = Recommendation(
        id="s1", title="Run batch-render after 10:35 (off_peak) instead of now", severity=Severity.NOTICE,
        action="Pause or reschedule batch-render until off_peak starts in 39m.",
        rationale="batch-render is drawing 1875 W in peak. 3.75 kWh at 52 c/kWh rather than 18 c/kWh saves about "
        "1.28 USD.", status=Status.APPROVED,
    )  # fmt: skip
    return Facts(
        kind="evening", at=AT, approved=[gpu, shift], tariff=TariffFact("peak", 52.0, "kWh", "USD", "off_peak", 2340, 2)
    )


def quiet_facts() -> Facts:
    return Facts(kind="status", at=AT)


INVENTED_CRITICAL = (
    'A warning is issued for the critical item "Power outage expected". It is recommended that the operator '
    "generates an emergency power source and schedules a backup plan. The recommended action is to act immediately. "
    "A critical event is anticipated. Nothing else is recommended or approved."
)
"""Real: a status report with nothing open at all."""

INVENTED_ITEM = (
    "Good evening. The most severe item is the GPU 0 on demo-gpu-node is running hot (92°C). It's recommended to "
    "check the GPU's fans and airflow or cap its power limit to prevent damage. The following recommendations are "
    "also awaiting a decision: GPU 1 on testnet-node is running low on memory (64MB). A person is planning to run "
    "it for 60 minutes. Run batch-render after 10:35 instead of now."
)
"""Real (trimmed): an evening review that added a GPU, a host and a plan that don't exist."""

GROUNDED = (
    "Good evening. GPU 0 on demo-gpu-node is critical at about 92°C: check its fans and airflow, or cap its power "
    "limit. batch-render is drawing about 1.9 kW at peak, 52 c/kWh; running it after 10:35, off-peak at 18 c/kWh, "
    "saves about 1.3 USD. Both are approved, for a person to carry out. Off-peak starts in 39 minutes."
)


def test_the_real_invented_critical_is_refused():
    verdict = check_completion(completion(INVENTED_CRITICAL), quiet_facts())
    assert verdict is not None and (verdict.reason, verdict.rule) == ("ungrounded", "overstates_severity")


def test_the_real_invented_item_is_refused_for_its_numbers():
    verdict = check_completion(completion(INVENTED_ITEM), demo_facts())
    assert verdict is not None and (verdict.reason, verdict.rule) == ("ungrounded", "unsupported_numbers")
    # "64" alone refuses it. "60" (minutes) passes: it is within the rounding tolerance of 59, from 09:59. The
    # check catches numbers nowhere near a fact, not every invention.
    assert verdict.missing == ("64",)


def test_a_grounded_answer_with_rounding_and_unit_changes_passes():
    assert unsupported_numbers(GROUNDED, demo_facts()) == []  # 1.9 kW for 1875 W; 1.3 for 1.28
    assert check_completion(completion(GROUNDED), demo_facts()) is None


@pytest.mark.parametrize(
    "text, refused",
    [
        ("Nothing is critical today; the scrub finished.", False),
        ("No critical items, and none are pending.", False),
        ("There is not a single critical item.", False),
        ("The pool is critical and needs attention now.", True),
        ("Critically, the pool needs attention now.", True),
    ],
)
def test_critical_is_refused_unless_negated_or_true(text, refused):
    assert overstated_severity(text, quiet_facts()) is refused
    assert not overstated_severity(text, demo_facts())  # a critical fact makes the word true


@pytest.mark.parametrize(
    "text, numbers",
    [
        ("two items, one hundred and five checks", [2.0, 105.0]),
        ("1,234 W at 10:35", [1234.0, 10.0, 35.0]),
        ("about 3.75 kWh, item #4, host-a2", [3.75, 4.0]),
    ],
)
def test_numbers_in(text, numbers):
    assert numbers_in(text) == numbers


def test_small_counts_dates_and_times_pass():
    text = "Saturday 28 November 2026, 09:59. Three recommendations, the second approved by a rule."
    assert unsupported_numbers(text, demo_facts()) == []


async def test_an_ungrounded_answer_falls_back_with_the_text_kept(tmp_path):
    from argus_scout.store import SqliteStore

    with SqliteStore(tmp_path / "scout.db") as store:
        report = await narrator(store, FakeGenerator(INVENTED_CRITICAL)).report("status")
    meta = report.meta
    assert meta["path"] == "template" and meta["fallback_reason"] == "ungrounded"
    assert meta["rejected_check"]["rule"] == "overstates_severity"
    assert meta["rejected_text"].startswith("A warning is issued")
    assert "Power outage" not in report.text and "the facts don't support" in report.text
