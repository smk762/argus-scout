"""Time-of-use energy tariffs.

A tariff names its periods by local wall-clock time. Prices are per ``unit``
(usually kWh) in the currency's minor unit (cents, pence), so the engine works
for any currency. See ``examples/tariff.yaml``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any

import structlog
import yaml

log = structlog.get_logger()

_MINUTES_PER_DAY = 24 * 60


def _parse_time(value: Any) -> time:
    return time.fromisoformat(str(value))


def _minutes(t: time) -> int:
    return t.hour * 60 + t.minute


@dataclass(frozen=True)
class TariffPeriod:
    name: str
    start: time
    end: time
    rate: float

    def contains_time(self, t: time) -> bool:
        """Whether ``t`` falls in this period: start inclusive, end exclusive.

        Handles overnight periods (21:00-09:00), and start == end means all day.
        """
        if self.start == self.end:
            return True
        if self.start < self.end:
            return self.start <= t < self.end
        return t >= self.start or t < self.end


@dataclass(frozen=True)
class TariffTier:
    """A block-rate tier: ``up_to_units`` caps it, ``None`` means everything above."""

    up_to_units: float | None
    rate: float


@dataclass(frozen=True)
class Plan:
    name: str
    daily_charge: float
    periods: tuple[TariffPeriod, ...] = ()
    tiers: tuple[TariffTier, ...] = ()


@dataclass(frozen=True)
class Tariff:
    plan: Plan
    currency: str
    unit: str
    comparison_plans: tuple[Plan, ...] = ()

    @property
    def periods(self) -> tuple[TariffPeriod, ...]:
        return self.plan.periods


def _periods(raw: dict[str, Any] | None) -> tuple[TariffPeriod, ...]:
    return tuple(
        TariffPeriod(name=str(name), start=_parse_time(p["start"]), end=_parse_time(p["end"]), rate=float(p["rate"]))
        for name, p in (raw or {}).items()
    )


def _tiers(raw: list[dict[str, Any]] | None) -> tuple[TariffTier, ...]:
    tiers = tuple(
        TariffTier(
            up_to_units=float(t["up_to_units"]) if t.get("up_to_units") is not None else None, rate=float(t["rate"])
        )
        for t in raw or []
    )
    caps = [t.up_to_units for t in tiers]
    if None in caps[:-1] or caps != sorted(caps, key=lambda c: float("inf") if c is None else c):
        raise ValueError("tiers must ascend by up_to_units, with only the last one open-ended")
    return tiers


def _plan(name: str, raw: dict[str, Any]) -> Plan:
    plan = Plan(
        name=name,
        daily_charge=float(raw.get("daily_charge", 0)),
        periods=_periods(raw.get("periods")),
        tiers=_tiers(raw.get("tiers")),
    )
    if not plan.periods and not plan.tiers:
        raise ValueError(f"plan {name!r} needs periods or tiers")
    return plan


def tariff_from_dict(raw: dict[str, Any]) -> Tariff:
    plan = _plan(str(raw["plan"]), raw)
    if not plan.periods:
        raise ValueError("the active plan needs time-of-use periods")
    return Tariff(
        plan=plan,
        currency=str(raw.get("currency", "")),
        unit=str(raw.get("unit", "kWh")),
        comparison_plans=tuple(_plan(str(n), p) for n, p in (raw.get("comparison_plans") or {}).items()),
    )


def load_tariff(path: str | Path) -> Tariff:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("tariff file must be a YAML mapping")
    return tariff_from_dict(raw)


class TariffFile:
    """A tariff backed by a file, re-read when its mtime changes.

    :meth:`get` returns None when no path is set, the file is missing, or it fails to load (``error`` says
    why): a bad tariff edit turns tariff awareness off rather than stopping anything.
    """

    def __init__(self, path: str | Path | None) -> None:
        self.path = str(path) if path else ""
        self.error: str | None = None
        self._mtime: float | None = None
        self._cached: Tariff | None = None

    def get(self) -> Tariff | None:
        if not self.path:
            return None
        try:
            mtime = Path(self.path).stat().st_mtime
        except OSError as exc:
            self.error, self._mtime, self._cached = str(exc), None, None
            return None
        if mtime == self._mtime:
            return self._cached
        try:
            self._cached, self.error = load_tariff(self.path), None
        except Exception as exc:  # noqa: BLE001 — degrade to "no tariff", report the error
            log.warning("tariff.load_failed", path=self.path, error=str(exc))
            self._cached, self.error = None, str(exc)
        self._mtime = mtime
        return self._cached


def coverage_gaps(periods: tuple[TariffPeriod, ...]) -> list[tuple[time, time]]:
    """Stretches of the day no period covers, as (start, end) pairs."""
    covered = [False] * _MINUTES_PER_DAY
    for minute in range(_MINUTES_PER_DAY):
        t = time(minute // 60, minute % 60)
        covered[minute] = any(p.contains_time(t) for p in periods)
    gaps: list[tuple[time, time]] = []
    minute = 0
    while minute < _MINUTES_PER_DAY:
        if covered[minute]:
            minute += 1
            continue
        start = minute
        while minute < _MINUTES_PER_DAY and not covered[minute]:
            minute += 1
        end = minute % _MINUTES_PER_DAY
        gaps.append((time(start // 60, start % 60), time(end // 60, end % 60)))
    return gaps


def current_period(tariff: Tariff, now: datetime | None = None) -> TariffPeriod:
    """The period active at ``now`` (local time). Outside every period, the dearest one."""
    t = (now or datetime.now()).time()
    for period in tariff.periods:
        if period.contains_time(t):
            return period
    return max(tariff.periods, key=lambda p: p.rate)


def next_period_change(tariff: Tariff, now: datetime | None = None) -> tuple[str, int]:
    """The next period's name and the seconds until it starts."""
    current = now or datetime.now()
    active = current_period(tariff, current)
    starts = sorted(tariff.periods, key=lambda p: _minutes(p.start))
    for day in (0, 1):
        for p in starts:
            if p.name == active.name:
                continue
            at = current.replace(hour=p.start.hour, minute=p.start.minute, second=0, microsecond=0) + timedelta(
                days=day
            )
            if at > current:
                return p.name, int((at - current).total_seconds())
    return active.name, 0  # a single all-day period never changes


def period_cost(units: float, period: TariffPeriod) -> float:
    """Cost of ``units`` in ``period``, in minor currency units."""
    return units * period.rate


def tiered_cost(units: float, tiers: tuple[TariffTier, ...]) -> float:
    """Cost of ``units`` over block-rate ``tiers``, in minor currency units.

    Without an open-ended last tier, units above the last cap are charged at the last tier's rate.
    """
    cost, floor = 0.0, 0.0
    for tier in tiers:
        cap = units if tier.up_to_units is None else min(units, tier.up_to_units)
        if cap > floor:
            cost += (cap - floor) * tier.rate
            floor = cap
        if tier.up_to_units is None or units <= tier.up_to_units:
            break
    else:
        if tiers and units > floor:
            cost += (units - floor) * tiers[-1].rate
    return cost
