from __future__ import annotations

from datetime import datetime, time
from pathlib import Path

import pytest

from argus_scout.tariff import (
    Tariff,
    TariffPeriod,
    TariffTier,
    coverage_gaps,
    current_period,
    load_tariff,
    next_period_change,
    tariff_from_dict,
    tiered_cost,
)

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


@pytest.fixture
def tariff() -> Tariff:
    return load_tariff(EXAMPLES / "tariff.yaml")


def at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 5, 9, hour, minute)


def test_load_example(tariff: Tariff):
    assert tariff.plan.name == "time_of_use"
    assert tariff.currency == "USD"
    assert {p.name for p in tariff.periods} == {"solar_soak", "peak", "off_peak"}
    assert [p.name for p in tariff.comparison_plans] == ["flat_rate", "block_rate"]
    assert coverage_gaps(tariff.periods) == []


@pytest.mark.parametrize(
    ("when", "name"),
    [
        (at(10), "solar_soak"),  # boundary: start is inclusive
        (at(12), "solar_soak"),
        (at(15), "peak"),
        (at(17), "peak"),
        (at(21), "off_peak"),
        (at(23), "off_peak"),
        (at(3), "off_peak"),
        (at(9, 59), "off_peak"),
    ],
)
def test_current_period(tariff: Tariff, when: datetime, name: str):
    assert current_period(tariff, when).name == name


def test_next_period_change_same_day(tariff: Tariff):
    assert next_period_change(tariff, at(14, 30)) == ("peak", 1800)


def test_next_period_change_wraps_to_tomorrow(tariff: Tariff):
    # 23:00 in off_peak: next is solar_soak at 10:00 tomorrow, 11h away.
    assert next_period_change(tariff, at(23)) == ("solar_soak", 11 * 3600)


def test_single_all_day_period_never_changes():
    flat = tariff_from_dict({"plan": "flat", "periods": {"all": {"start": "00:00", "end": "00:00", "rate": 30}}})
    assert current_period(flat, at(13)).name == "all"
    assert next_period_change(flat, at(13)) == ("all", 0)


def test_overnight_period_contains():
    off_peak = TariffPeriod("off_peak", time(21, 0), time(9, 0), 23.0)
    assert off_peak.contains_time(time(23, 0))
    assert off_peak.contains_time(time(3, 0))
    assert off_peak.contains_time(time(8, 59))
    assert not off_peak.contains_time(time(9, 0))
    assert not off_peak.contains_time(time(15, 0))


def test_gap_detected_and_dearest_period_used_inside_it():
    gappy = tariff_from_dict(
        {
            "plan": "gappy",
            "periods": {
                "cheap": {"start": "00:00", "end": "07:00", "rate": 10},
                "dear": {"start": "09:00", "end": "00:00", "rate": 40},
            },
        }
    )
    assert coverage_gaps(gappy.periods) == [(time(7, 0), time(9, 0))]
    assert current_period(gappy, at(8)).name == "dear"


def test_active_plan_needs_periods():
    with pytest.raises(ValueError, match="time-of-use periods"):
        tariff_from_dict({"plan": "tiers-only", "tiers": [{"rate": 30}]})


def test_tiers_must_ascend():
    with pytest.raises(ValueError, match="ascend"):
        tariff_from_dict(
            {
                "plan": "p",
                "periods": {"all": {"start": "00:00", "end": "00:00", "rate": 1}},
                "comparison_plans": {"bad": {"tiers": [{"rate": 1}, {"up_to_units": 5, "rate": 2}]}},
            }
        )


def test_tiered_cost(tariff: Tariff):
    tiers = tariff.comparison_plans[1].tiers
    assert tiered_cost(10, tiers) == 10 * 32.0
    assert tiered_cost(100, tiers) == 20 * 32.0 + 80 * 30.0
    assert tiered_cost(2000, tiers) == 20 * 32.0 + 1480 * 30.0 + 500 * 34.0
    assert tiered_cost(0, (TariffTier(None, 5.0),)) == 0


# --- showing rates --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("rate", "currency", "shown"),
    [
        (23.6916, "AUD", "23.7 c/kWh"),
        (48.0, "USD", "48 c/kWh"),
        (9.04, "usd", "9 c/kWh"),
        (0.25, "USD", "0.25 c/kWh"),
        (31.46, "JPY", "31.5 JPY/kWh"),  # no minor unit: the rate is already in yen
        (24.5, "GBP", "24.5 p/kWh"),
        (23.7, "CHF", "0.237 CHF/kWh"),  # no common minor-unit sign: major units
        (24.0, "KWD", "0.024 KWD/kWh"),  # three-decimal currency
        (23.6916, "", "23.7/kWh"),
    ],
)
def test_format_rate(rate, currency, shown):
    from argus_scout.tariff import format_rate

    assert format_rate(rate, currency) == shown


def test_minor_digits_from_the_currency_or_the_file():
    from argus_scout.tariff import format_money, minor_digits

    assert (minor_digits("AUD"), minor_digits("JPY"), minor_digits("KWD")) == (2, 0, 3)
    assert format_money(13260, "AUD") == "132.60" and format_money(260, "JPY") == "260"
    raw = {"plan": "p", "currency": "XTS", "minor_digits": 0, "periods": {"a": {"start": "00:00", "end": "00:00",
                                                                            "rate": 12.34}}}  # fmt: skip
    tariff = tariff_from_dict(raw)
    assert tariff.format_rate(12.34) == "12.3 XTS/kWh"
    with pytest.raises(ValueError, match="minor_digits"):
        tariff_from_dict({**raw, "minor_digits": "two"})
