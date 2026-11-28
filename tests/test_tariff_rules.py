from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import yaml

from argus_scout.cycles import AnalysisCycle
from argus_scout.engine import RecommendationEngine
from argus_scout.prometheus import PrometheusUnavailable, Sample, Series
from argus_scout.recommendations import Status
from argus_scout.rules_file import SignalRulesFile, parse_rules
from argus_scout.severity import Severity
from argus_scout.signals import SignalRule, SignalState
from argus_scout.store import SqliteStore
from argus_scout.tariff import Plan, TariffFile, TariffPeriod, TariffTier, tariff_from_dict
from argus_scout.tariff_rules import (
    TariffPlanRule,
    TariffShiftRule,
    cheaper_ahead,
    comparable_plans,
    energy_kwh,
    evaluate_tariff,
    format_money,
    plan_cost,
    tariff_problem,
    usage_points,
)

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
DAY = datetime(2026, 3, 2, tzinfo=UTC)
PEAK = DAY.replace(hour=16)  # the example tariff: peak 15-21 (48), off_peak 21-10 (22), solar_soak 10-15 (9)
SOLAR = DAY.replace(hour=11)
LOAD = {"load": "batch-jobs"}


@pytest.fixture
def tariff():
    return tariff_from_dict(yaml.safe_load((EXAMPLES / "tariff.yaml").read_text()))


@pytest.fixture
def store():
    with SqliteStore(":memory:") as s:
        yield s


class FakeProm:
    """``instant``: [(labels, value)] or an exception; ``points``: [(at, value)] per series, or an exception."""

    def __init__(self, instant=None, points=None) -> None:
        self.instant = instant or []
        self.points = points or []
        self.queries: list[str] = []
        self.ranges: list[tuple[datetime, datetime, timedelta]] = []

    async def query(self, promql: str, *, at: datetime | None = None) -> list[Sample]:
        self.queries.append(promql)
        if isinstance(self.instant, Exception):
            raise self.instant
        return [Sample(labels, value, at or PEAK) for labels, value in self.instant]

    async def query_range(self, promql: str, *, start, end, step) -> list[Series]:
        self.ranges.append((start, end, step))
        if isinstance(self.points, Exception):
            raise self.points
        return [Series({}, list(points)) for points in self.points]


def shift_raw(**extra) -> dict:
    raw = {
        "name": "shift-jobs",
        "kind": "tariff_shift",
        "query": "load_power_watts",
        "unit": "W",
        "min_shiftable": 300,
        "duration": "2h",
        "periods": ["peak"],
        "horizon": "8h",
        "labels_key": ["load"],
        "recommendation": {
            "title": "Run {load} after {starts_at} ({cheaper_period})",
            "action": "Wait {starts_in} for {cheaper_period}.",
            "rationale": "{energy} kWh at {rate} not {cheaper_rate} saves {saving} {currency} ({value:.0f} {unit}).",
        },
    }
    raw.update(extra)
    return raw


def shift_rule(**extra) -> TariffShiftRule:
    return TariffShiftRule.model_validate(shift_raw(**extra))


def plan_raw(**extra) -> dict:
    raw = {
        "name": "cheaper-plan",
        "kind": "tariff_plan",
        "query": "energy_kwh",
        "recommendation": {
            "title": "{plan} saves {saving} {currency} over {window}",
            "action": "Compare {plan} with {current_plan}.",
            "rationale": "{usage} kWh in {days} days: {current_cost} vs {plan_cost}.",
        },
    }
    raw.update(extra)
    return raw


def plan_rule(**extra) -> TariffPlanRule:
    return TariffPlanRule.model_validate(plan_raw(**extra))


def hourly(end: datetime, days: int, kwh_at) -> list[tuple[datetime, float]]:
    """One point per hour for ``days`` up to ``end``; each is the energy of the hour ending at it."""
    return [(at, kwh_at(at)) for at in (end - timedelta(hours=h) for h in range(days * 24 - 1, -1, -1))]


def peak_only(at: datetime) -> float:
    return 4.0 if 15 <= (at - timedelta(minutes=30)).hour < 21 else 0.0


# --- schema ---------------------------------------------------------------------------------------------------


def test_kinds_are_discriminated_and_threshold_is_the_default():
    threshold = {"name": "t", "query": "q", "threshold": 1, "recommendation": {"title": "x", "action": "y"}}
    ruleset = parse_rules({"rules": [threshold, shift_raw(), plan_raw()]})
    assert ruleset.errors == []
    assert [type(r) for r in ruleset.rules] == [SignalRule, TariffShiftRule, TariffPlanRule]
    assert ruleset.rules[0].kind == "threshold"


def test_bad_tariff_rules_are_skipped_with_reasons():
    ruleset = parse_rules(
        {
            "rules": [
                {**shift_raw(), "name": "a", "kind": "tariff_magic"},
                shift_raw(name="b", horizon="2d"),
                shift_raw(name="c", periods=[]),
                shift_raw(name="d", threshold=5),  # a threshold-only key
                plan_raw(name="e", every="6h"),
                plan_raw(name="f", window="400d", step="1m"),
                plan_raw(name="g", recommendation={"title": "{load}", "action": "x"}),
                shift_raw(name="ok"),
            ]
        }
    )
    assert [r.name for r in ruleset.rules] == ["ok"]
    text = "\n".join(ruleset.errors)
    assert "unknown kind 'tariff_magic'" in text
    assert "horizon must be more than 0 and at most 1d" in text
    assert "rule 3 (d): threshold: unknown key" in text
    assert "every must be at least 1d" in text
    assert "window / step" in text
    assert "{load} is not a reserved name" in text
    assert len(ruleset.errors) == 7


def test_example_files_load(tariff):
    ruleset = SignalRulesFile(EXAMPLES / "signals.yaml").get()
    assert ruleset.errors == [] and ruleset.file_error is None
    kinds = {r.kind for r in ruleset.rules}
    assert kinds == {"threshold", "tariff_shift", "tariff_plan"}
    assert [p.name for p in tariff.comparison_plans] == ["flat_rate", "block_rate"]


# --- maths ----------------------------------------------------------------------------------------------------


def test_money_and_energy():
    assert format_money(26) == "0.26"
    assert format_money(13260, "USD") == "132.60"
    assert format_money(260, "JPY") == "260"
    assert energy_kwh(500, "W", timedelta(hours=2)) == pytest.approx(1.0)
    assert energy_kwh(1.5, "kW", timedelta(minutes=30)) == pytest.approx(0.75)
    assert energy_kwh(3.0, "kWh", timedelta(hours=9)) == 3.0


def test_cheapest_period_ahead_wins_not_the_next(tariff):
    three = tariff_from_dict(
        {
            "plan": "tou",
            "periods": {
                "peak": {"start": "16:00", "end": "20:00", "rate": 50},
                "shoulder": {"start": "20:00", "end": "22:00", "rate": 30},
                "night": {"start": "22:00", "end": "16:00", "rate": 10},
            },
        }
    )
    at = DAY.replace(hour=19)
    current, cheaper, starts = cheaper_ahead(three, at, timedelta(hours=4))
    assert (current.name, cheaper.name, starts.hour) == ("peak", "night", 22)
    _, cheaper, _ = cheaper_ahead(three, at, timedelta(hours=2))
    assert cheaper.name == "shoulder"
    assert cheaper_ahead(three, at, timedelta(minutes=30)) is None
    assert cheaper_ahead(tariff, SOLAR, timedelta(hours=24)) is None  # nothing is cheaper than solar_soak


def test_plan_cost_by_period_and_by_tier(tariff):
    usage = usage_points([Series({}, hourly(DAY, 1, lambda at: 1.0))], timedelta(hours=1), "kWh")
    tou = plan_cost(tariff.plan, usage, timedelta(hours=1), UTC, days=1)
    assert tou.energy == pytest.approx(5 * 9 + 6 * 48 + 13 * 22) and tou.standing == 110
    flat, block = tariff.comparison_plans
    assert plan_cost(flat, usage, timedelta(hours=1), UTC, days=1).total == pytest.approx(24 * 30 + 100)
    assert plan_cost(block, usage, timedelta(hours=1), UTC, days=1).total == pytest.approx(20 * 32 + 4 * 30 + 180)
    capped = Plan("capped", 0, tiers=(TariffTier(10, 5.0), TariffTier(20, 7.0)))
    # Above the last cap, units are charged at the last tier's rate.
    assert plan_cost(capped, usage, timedelta(hours=1), UTC, days=1).total == pytest.approx(10 * 5 + 14 * 7)


def test_usage_points_sum_series_and_convert_power():
    t1, t2 = DAY, DAY + timedelta(hours=1)
    series = [Series({}, [(t1, 1000.0), (t2, float("nan"))]), Series({}, [(t1, 500.0), (t2, 2000.0)])]
    assert usage_points(series, timedelta(hours=1), "W") == [(t1, 1.5), (t2, 2.0)]


def test_comparison_plans_with_gaps_are_left_out(tariff):
    gappy = Plan("gappy", 0, periods=(TariffPeriod("day", time(8), time(20), 1.0),))
    plans, left_out = comparable_plans(tariff.__class__(tariff.plan, "USD", "kWh", (gappy,)))
    assert [p.name for p in plans] == ["time_of_use"] and left_out == ["gappy: periods leave gaps"]


# --- load shifting --------------------------------------------------------------------------------------------


async def test_shift_fires_in_an_expensive_period_with_a_cheaper_one_ahead(tariff):
    prom = FakeProm([(LOAD, 500.0)])
    result = await evaluate_tariff([shift_rule()], prom, tariff, PEAK)
    [hit] = result
    assert hit.key == 'shift-jobs{load="batch-jobs"}' and hit.severity == Severity.NOTICE
    assert hit.title == "Run batch-jobs after 21:00 (off_peak)"
    assert hit.action == "Wait 5h for off_peak."
    # 500 W for 2h = 1 kWh; (48 - 22) minor units per kWh = 26 = 0.26 in major units.
    assert hit.rationale == "1.00 kWh at 48 c/kWh not 22 c/kWh saves 0.26 USD (500 W)."
    assert result.evaluated == {"shift-jobs"} and not result.unknown


async def test_shift_is_clear_outside_its_periods_or_with_nothing_cheaper_soon(tariff):
    prom = FakeProm([(LOAD, 5000.0)])
    off = await evaluate_tariff([shift_rule()], prom, tariff, SOLAR)
    assert list(off) == [] and off.evaluated == {"shift-jobs"}
    near = await evaluate_tariff([shift_rule(horizon="2h")], prom, tariff, PEAK)  # off_peak is 5h away
    assert list(near) == [] and near.evaluated == {"shift-jobs"}
    assert prom.queries == []  # neither needed Prometheus


async def test_shift_minimums(tariff):
    prom = FakeProm([(LOAD, 500.0), ({"load": "tiny"}, 100.0)])
    [hit] = await evaluate_tariff([shift_rule()], prom, tariff, PEAK)
    assert hit.labels == LOAD  # tiny is under min_shiftable
    assert list(await evaluate_tariff([shift_rule(min_saving=27)], prom, tariff, PEAK)) == []
    [kwh] = await evaluate_tariff([shift_rule(unit="kWh", min_shiftable=2)], FakeProm([(LOAD, 3.0)]), tariff, PEAK)
    assert "3.00 kWh" in kwh.rationale and "0.78 USD" in kwh.rationale  # 3 x 26


async def test_shift_uses_the_configured_timezone(tariff):
    brisbane = ZoneInfo("Australia/Brisbane")  # UTC+10, no daylight saving
    at = DAY.replace(hour=6)  # 16:00 local: peak
    [hit] = await evaluate_tariff([shift_rule()], FakeProm([(LOAD, 500.0)]), tariff, at, tz=brisbane)
    assert hit.title.endswith("after 21:00 (off_peak)")
    assert list(await evaluate_tariff([shift_rule()], FakeProm([(LOAD, 500.0)]), tariff, at)) == []  # 06:00 UTC


async def test_shift_for_duration_and_dedup_across_cycles(tariff, store):
    rule = shift_rule(**{"for": "10m"})
    prom, state = FakeProm([(LOAD, 500.0)]), SignalState(store)
    engine = RecommendationEngine(store, None, clock=lambda: now)
    now = PEAK
    first = await evaluate_tariff([rule], prom, tariff, now, state=state)
    assert list(first) == [] and first.pending == 1
    now = PEAK + timedelta(minutes=10)
    report = engine.run_cycle(await evaluate_tariff([rule], prom, tariff, now, state=SignalState(store)))
    assert len(report.new) == 1 and report.new[0].status == Status.PROPOSED
    now = PEAK + timedelta(minutes=25)
    report = engine.run_cycle(await evaluate_tariff([rule], prom, tariff, now, state=state))
    assert report.new == [] and len(report.refreshed) == 1
    assert len(store.recommendations_by_key('shift-jobs{load="batch-jobs"}')) == 1


async def test_shift_unknown_and_skipped(tariff):
    down = await evaluate_tariff([shift_rule()], FakeProm(PrometheusUnavailable("down")), tariff, PEAK)
    assert list(down) == [] and down.evaluated == set() and down.unknown == {"shift-jobs": "down"}
    none = await evaluate_tariff([shift_rule()], FakeProm(), None, PEAK, tariff_error="bad yaml")
    assert none.unknown == {"shift-jobs": "skipped: no tariff loaded (bad yaml)"}
    gappy = tariff_from_dict({"plan": "p", "periods": {"peak": {"start": "08:00", "end": "20:00", "rate": 1}}})
    gaps = await evaluate_tariff([shift_rule()], FakeProm(), gappy, PEAK)
    assert gaps.unknown["shift-jobs"] == "skipped: tariff periods leave gaps: 00:00-08:00, 20:00-00:00"
    wrong = await evaluate_tariff([shift_rule(periods=["evening"])], FakeProm(), tariff, PEAK)
    assert wrong.unknown["shift-jobs"] == "skipped: the tariff has no period evening"
    assert (
        tariff_problem(tariff.__class__(tariff.plan, "USD", "MWh"))
        == "tariff is priced per 'MWh'; tariff rules need kWh"
    )


# --- plan comparison ------------------------------------------------------------------------------------------


async def test_plan_comparison_recommends_the_cheapest(tariff):
    prom = FakeProm(points=[hourly(PEAK.replace(hour=0), 30, peak_only)])
    [hit] = await evaluate_tariff([plan_rule()], prom, tariff, PEAK.replace(hour=0))
    # 24 kWh a day, all in peak: time_of_use 24x48 + 110 = 1262/day; flat_rate 24x30 + 100 = 820/day;
    # block_rate (720 kWh over the window) 20x32 + 700x30 + 30x180 = 27040. Flat saves (1262 - 820) x 30.
    assert hit.key == 'cheaper-plan{plan="flat_rate"}' and hit.severity == Severity.INFO
    assert hit.value == pytest.approx(13260)
    assert hit.title == "flat_rate saves 132.60 USD over 30d"
    assert hit.rationale == "720.0 kWh in 30.0 days: 378.60 vs 246.00."
    assert prom.ranges == [(PEAK.replace(hour=0) - timedelta(days=30), PEAK.replace(hour=0), timedelta(hours=1))]


async def test_plan_comparison_respects_the_threshold_and_the_active_plan(tariff):
    end = PEAK.replace(hour=0)
    prom = FakeProm(points=[hourly(end, 30, peak_only)])
    over = await evaluate_tariff([plan_rule(min_saving=13261)], prom, tariff, end)
    assert list(over) == [] and over.evaluated == {"cheaper-plan"}
    flat_use = FakeProm(points=[hourly(end, 30, lambda at: 1.0)])  # 729/day on time_of_use: already cheapest
    same = await evaluate_tariff([plan_rule()], flat_use, tariff, end)
    assert list(same) == [] and same.evaluated == {"cheaper-plan"}


async def test_plan_comparison_runs_at_most_daily_even_across_restarts(tariff, store):
    end = PEAK.replace(hour=0)
    prom = FakeProm(points=[hourly(end, 30, peak_only)])
    assert len(await evaluate_tariff([plan_rule()], prom, tariff, end, state=SignalState(store))) == 1
    later = await evaluate_tariff([plan_rule()], prom, tariff, end + timedelta(hours=23), state=SignalState(store))
    assert list(later) == [] and later.evaluated == set() and later.unknown == {}  # not due: says nothing
    assert len(prom.ranges) == 1
    due = await evaluate_tariff([plan_rule()], prom, tariff, end + timedelta(days=1), state=SignalState(store))
    assert len(due) == 1 and len(prom.ranges) == 2


async def test_plan_comparison_refreshes_rather_than_spams(tariff, store):
    end = PEAK.replace(hour=0)
    prom, state = FakeProm(points=[hourly(end, 30, peak_only)]), SignalState(store)
    now = end
    engine = RecommendationEngine(store, None, clock=lambda: now)
    assert len(engine.run_cycle(await evaluate_tariff([plan_rule()], prom, tariff, now, state=state)).new) == 1
    for hours in (2, 12, 23):  # cycles between runs leave it alone, past the engine's clear grace
        now = end + timedelta(hours=hours)
        report = engine.run_cycle(await evaluate_tariff([plan_rule()], prom, tariff, now, state=state))
        assert not report.changed
    now = end + timedelta(days=1)
    report = engine.run_cycle(await evaluate_tariff([plan_rule()], prom, tariff, now, state=state))
    assert report.new == [] and len(report.refreshed) == 1
    assert len(store.list_recommendations(Status.PROPOSED)) == 1


async def test_plan_comparison_unknown_paths(tariff, store):
    end = PEAK.replace(hour=0)
    state = SignalState(store)
    down = await evaluate_tariff(
        [plan_rule()], FakeProm(points=PrometheusUnavailable("down")), tariff, end, state=state
    )
    assert down.unknown == {"cheaper-plan": "down"}
    # An outage isn't a run: the next cycle tries again.
    thin = FakeProm(points=[hourly(end, 10, peak_only)])
    result = await evaluate_tariff([plan_rule()], thin, tariff, end + timedelta(minutes=15), state=state)
    assert result.unknown == {"cheaper-plan": "not enough data: 33% of 30d (needs 90%)"} and len(thin.ranges) == 1
    skipped = await evaluate_tariff([plan_rule()], FakeProm(), None, end)
    assert skipped.unknown == {"cheaper-plan": "skipped: no tariff loaded"}


# --- through the analysis cycle -------------------------------------------------------------------------------

SIGNALS = """
rules:
  - name: disk-filling
    query: disk_used_percent
    threshold: 85
    labels_key: [instance]
    recommendation: {title: "{instance} disk", action: free space}
  - name: shift-jobs
    kind: tariff_shift
    query: load_power_watts
    min_shiftable: 300
    duration: 2h
    periods: [peak]
    labels_key: [load]
    recommendation:
      title: "Run {load} in {cheaper_period}"
      action: "Saves {saving} {currency}."
"""


class CycleProm:
    def __init__(self) -> None:
        self.answers = {
            "disk_used_percent": [({"instance": "node-a:9100"}, 90.0)],
            "load_power_watts": [(LOAD, 500.0)],
        }

    async def query(self, promql: str, *, at: datetime | None = None) -> list[Sample]:
        return [Sample(labels, value, at or PEAK) for labels, value in self.answers.get(promql, [])]


async def test_analysis_cycle_merges_tariff_rules(store, write_file):
    rules = SignalRulesFile(write_file("signals.yaml", SIGNALS))
    tariff = TariffFile(EXAMPLES / "tariff.yaml")
    cycle = AnalysisCycle(store, rules=rules, client=CycleProm(), tariff=tariff, clock=lambda: PEAK)
    report = await cycle()
    assert sorted(r.key for r in report.new) == [
        'disk-filling{instance="node-a:9100"}',
        'shift-jobs{load="batch-jobs"}',
    ]
    shift = next(r for r in report.new if r.analyzer == "shift-jobs")
    assert shift.title == "Run batch-jobs in off_peak" and shift.action == "Saves 0.26 USD."
    assert shift.status == Status.PROPOSED


async def test_analysis_cycle_without_a_tariff_skips_only_tariff_rules(store, write_file, tmp_path):
    rules = SignalRulesFile(write_file("signals.yaml", SIGNALS))
    cycle = AnalysisCycle(
        store, rules=rules, client=CycleProm(), tariff=TariffFile(tmp_path / "missing.yaml"), clock=lambda: PEAK
    )
    report = await cycle()
    assert [r.analyzer for r in report.new] == ["disk-filling"]
    assert report.unknown["shift-jobs"].startswith("skipped: no tariff loaded")


async def test_analysis_cycle_keeps_tariff_rule_for_clocks(store, write_file):
    signals = SIGNALS.replace("    duration: 2h\n", "    duration: 2h\n    for: 10m\n")
    now = [PEAK]
    cycle = AnalysisCycle(
        store,
        rules=SignalRulesFile(write_file("signals.yaml", signals)),
        client=CycleProm(),
        tariff=TariffFile(EXAMPLES / "tariff.yaml"),
        clock=lambda: now[0],
    )
    first = await cycle()
    assert [r.analyzer for r in first.new] == ["disk-filling"]
    assert set(cycle.state.pending("shift-jobs")) == {'shift-jobs{load="batch-jobs"}'}  # threshold eval kept it
    now[0] = PEAK + timedelta(minutes=10)
    second = await cycle()
    assert [r.analyzer for r in second.new] == ["shift-jobs"]
