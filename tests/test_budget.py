from __future__ import annotations

from datetime import date

import pytest

from argus_scout.budget import (
    BudgetExhaustedError,
    DailyBudget,
    ManualOriginMiddleware,
    is_manual,
    reset_manual,
    set_manual,
)


def test_unlimited_by_default():
    budget = DailyBudget()
    for _ in range(100):
        assert budget.try_consume("llm")
    assert budget.used("llm") == 100
    assert not budget.exhausted("llm")


def test_background_refused_at_limit():
    budget = DailyBudget({"tts": 2})
    assert budget.try_consume("tts")
    assert budget.try_consume("tts")
    assert budget.exhausted("tts")
    assert not budget.available("tts")
    assert not budget.try_consume("tts")
    with pytest.raises(BudgetExhaustedError, match="daily tts limit reached"):
        budget.consume("tts")
    assert budget.used("tts") == 2


def test_manual_allowed_over_limit_unless_blocked():
    budget = DailyBudget({"speak": 1})
    budget.consume("speak")
    token = set_manual(True)
    try:
        assert budget.available("speak")
        assert budget.try_consume("speak")
        assert budget.used("speak") == 2
        budget.block_manual = True
        assert not budget.available("speak")
        assert not budget.try_consume("speak")
    finally:
        reset_manual(token)


def test_resets_at_local_midnight():
    day = [date(2026, 11, 20)]
    budget = DailyBudget({"llm": 1}, today=lambda: day[0])
    budget.consume("llm")
    assert budget.exhausted("llm")
    day[0] = date(2026, 11, 21)
    assert not budget.exhausted("llm")
    assert budget.snapshot()["day"] == "2026-11-21"


def test_counts_survive_restart(tmp_path):
    state = tmp_path / "budget.json"
    budget = DailyBudget({"llm": 3}, state_path=state)
    budget.consume("llm")
    budget.consume("llm")
    assert DailyBudget({"llm": 3}, state_path=state).used("llm") == 2


def test_corrupt_state_starts_from_zero(tmp_path):
    state = tmp_path / "budget.json"
    state.write_text("{not json")
    assert DailyBudget(state_path=state).used("llm") == 0


def test_snapshot_and_on_change():
    seen = []
    budget = DailyBudget({"tts": 5}, on_change=lambda counts, limits: seen.append((counts, limits)))
    budget.consume("tts")
    assert budget.snapshot()["kinds"]["tts"] == {"used": 1, "limit": 5, "exhausted": False}
    assert seen[-1] == ({"tts": 1}, {"tts": 5})


async def test_middleware_marks_requests_manual():
    seen = []

    async def app(scope, receive, send):
        seen.append(is_manual())

    await ManualOriginMiddleware(app)({"type": "http"}, None, None)
    await ManualOriginMiddleware(app)({"type": "lifespan"}, None, None)
    assert seen == [True, False]
    assert not is_manual()
