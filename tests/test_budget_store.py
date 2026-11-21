"""Daily usage counts kept in the store: they survive restarts and hold across processes sharing the database."""

from __future__ import annotations

import json
import multiprocessing
import threading
from datetime import date

import pytest

from argus_scout.budget import USAGE_STATE_FILE, DailyBudget, budget_from_config, reset_manual, set_manual
from argus_scout.config import ScoutConfig
from argus_scout.store import SqliteStore


def store_budget(path, limits, **kw) -> tuple[SqliteStore, DailyBudget]:
    store = SqliteStore(path)
    return store, DailyBudget(limits, counter=store, **kw)


def test_store_counts_survive_restart(tmp_path):
    store, budget = store_budget(tmp_path / "scout.db", {"llm_calls": 3})
    budget.consume("llm_calls")
    budget.consume("tts")
    store.close()
    store, again = store_budget(tmp_path / "scout.db", {"llm_calls": 3})
    assert (again.used("llm_calls"), again.used("tts")) == (1, 1)
    assert again.snapshot()["kinds"]["llm_calls"] == {"used": 1, "limit": 3, "exhausted": False}
    store.close()


def test_store_counts_reset_at_local_midnight(tmp_path):
    day = [date(2026, 11, 20)]
    store, budget = store_budget(tmp_path / "scout.db", {"speak": 1}, today=lambda: day[0])
    budget.consume("speak")
    assert not budget.try_consume("speak")
    day[0] = date(2026, 11, 21)
    assert budget.try_consume("speak") and budget.used("speak") == 1
    store.close()


def test_store_manual_over_limit_unless_blocked(tmp_path):
    store, budget = store_budget(tmp_path / "scout.db", {"tts": 1})
    budget.consume("tts")
    token = set_manual(True)
    try:
        assert budget.try_consume("tts") and budget.used("tts") == 2
        budget.block_manual = True
        assert not budget.try_consume("tts") and budget.used("tts") == 2
    finally:
        reset_manual(token)
    store.close()


def _hammer(path: str, limit: int, attempts: int, results) -> None:
    """One process (the server, or a CLI run): its own connection, charging as fast as it can."""
    store, budget = store_budget(path, {"llm_calls": limit})
    granted = sum(budget.try_consume("llm_calls") for _ in range(attempts))
    for _ in range(attempts):
        budget.consume("tts")  # unlimited: every one must be counted
        budget.consume("speak")
    store.close()
    results.put(granted)


@pytest.mark.parametrize("limit", [25, 0])
def test_two_processes_lose_no_count_and_never_exceed_the_cap(tmp_path, limit):
    path, attempts = str(tmp_path / "scout.db"), 40
    SqliteStore(path).close()  # migrate once, before the race
    ctx = multiprocessing.get_context("spawn")
    results = ctx.Queue()
    procs = [ctx.Process(target=_hammer, args=(path, limit, attempts, results)) for _ in range(2)]
    for p in procs:
        p.start()
    granted = [results.get(timeout=60) for _ in procs]
    for p in procs:
        p.join(timeout=60)
        assert p.exitcode == 0
    store, budget = store_budget(path, {})
    assert sum(granted) == budget.used("llm_calls") == (limit or 2 * attempts)
    assert budget.used("tts") == budget.used("speak") == 2 * attempts
    store.close()


def test_four_connections_share_one_cap(tmp_path):
    path = tmp_path / "scout.db"
    SqliteStore(path).close()
    granted: list[bool] = []

    def run() -> None:
        store, budget = store_budget(path, {"speak": 30})
        granted.extend([budget.try_consume("speak") for _ in range(50)])
        store.close()

    threads = [threading.Thread(target=run) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    store, budget = store_budget(path, {})
    assert sum(granted) == budget.used("speak") == 30
    store.close()


def test_server_and_cli_count_in_the_store(tmp_path):
    """The server's budget (which the narrative and the dispatcher's tts/speak share) and a CLI run's are one count."""
    from argus_scout.server.app import build_runtime

    config = ScoutConfig.model_validate({"store": {"path": str(tmp_path / "scout.db")}})
    with SqliteStore(config.store.path) as server_store, SqliteStore(config.store.path) as cli_store:
        runtime = build_runtime(config, store=server_store)
        assert runtime.budget.counter is server_store
        runtime.budget.consume("tts")
        assert budget_from_config(config, counter=cli_store).used("tts") == 1


def test_usage_json_is_imported_once(tmp_path):
    today = date.today().isoformat()
    legacy = tmp_path / USAGE_STATE_FILE
    legacy.write_text(json.dumps({"day": today, "counts": {"llm_calls": 4, "tts": 2}}))
    config = ScoutConfig.model_validate({"store": {"path": str(tmp_path / "scout.db")}})

    with SqliteStore(":memory:") as dry:
        assert budget_from_config(config, counter=dry, persist=False).used("llm_calls") == 4
    assert legacy.exists()  # a dry run leaves the old file in place

    with SqliteStore(tmp_path / "scout.db") as store:
        budget = budget_from_config(config, counter=store)
        assert (budget.used("llm_calls"), budget.used("tts")) == (4, 2)
        assert not legacy.exists() and (tmp_path / (USAGE_STATE_FILE + ".migrated")).exists()
        budget.consume("llm_calls")
        legacy.write_text(json.dumps({"day": today, "counts": {"llm_calls": 4}}))  # a stale copy reappears
        assert budget_from_config(config, counter=store).used("llm_calls") == 5  # merged by max, never added


def test_caps_reset_at_midnight_in_the_schedule_timezone(tmp_path):
    """The day rolls over at midnight in schedule.timezone, not at the host's (or UTC) midnight."""
    from datetime import UTC, datetime, timedelta

    config = ScoutConfig.model_validate(
        {
            "store": {"path": str(tmp_path / "scout.db")},
            "schedule": {"timezone": "Pacific/Auckland"},
            "budget": {"limits": {"tts": 1}},
        }  # fmt: skip
    )
    now = [datetime(2026, 6, 1, 11, 30, tzinfo=UTC)]  # 23:30 in Auckland (UTC+12), still 1 June there
    with SqliteStore(config.store.path) as store:
        budget = budget_from_config(config, counter=store, clock=lambda: now[0])
        assert budget.try_consume("tts") and not budget.try_consume("tts")
        now[0] += timedelta(minutes=20)  # 23:50 Auckland, 11:50 UTC: same day in both
        assert not budget.try_consume("tts")
        now[0] += timedelta(minutes=20)  # 00:10 on 2 June in Auckland, still 1 June in UTC
        assert budget.try_consume("tts"), "the cap must reset at Auckland midnight"
        assert store.usage_counts("2026-06-02") == {"tts": 1}


def test_caps_follow_the_schedule_timezone_on_the_wall_clock(tmp_path, monkeypatch):
    """Without a clock seam the wall clock is read in schedule.timezone, never the host's local zone."""
    from datetime import UTC, datetime

    import argus_scout.budget as budget_module

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            moment = datetime(2026, 6, 1, 23, 30, tzinfo=UTC)  # 2 June 11:30 in Auckland
            return moment.astimezone(tz) if tz else moment.replace(tzinfo=None)

    monkeypatch.setattr(budget_module, "datetime", Frozen)
    config = ScoutConfig.model_validate(
        {"store": {"path": str(tmp_path / "scout.db")}, "schedule": {"timezone": "Pacific/Auckland"}}
    )
    with SqliteStore(config.store.path) as store:
        budget = budget_from_config(config, counter=store)
        budget.consume("tts")
        assert store.usage_counts("2026-06-02") == {"tts": 1}
