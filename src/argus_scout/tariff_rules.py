"""Tariff-aware rules: shift load out of expensive periods, and compare plans from real usage.

Two rule kinds live in the signals file beside the threshold rules (``kind:`` picks one; a rule without
it is a threshold rule). Both read Prometheus and the tariff file (``files.tariff``), and produce
:class:`~argus_scout.signals.SignalHit` s that merge into the analysis cycle's
:class:`~argus_scout.signals.Evaluation`, so dedup, expiry, standing approvals (and their guardrails) and
rejection memory all apply unchanged. They recommend; they never act. ::

    rules:
      - name: shift-batch-jobs
        kind: tariff_shift
        query: sum by (load) (load_power_watts{shiftable="true"})
        unit: W                  # W | kW (power, run for `duration`) or kWh (energy left to run)
        min_shiftable: 300       # in `unit`: smaller loads aren't worth moving
        duration: 2h             # power units only: how long the load would keep running
        periods: [peak]          # the expensive periods this rule watches
        horizon: 6h              # a cheaper period must start within this (up to 24h)
        min_saving: 20           # optional, in the currency's minor unit
        for: 15m
        severity: notice
        labels_key: [load]
        recommendation:
          title: "Run {load} after {starts_at} ({cheaper_period})"
          action: Pause or reschedule {load} until {cheaper_period} starts in {starts_in}.
          rationale: "{energy} kWh at {rate} instead of {cheaper_rate} per kWh saves about {saving} {currency}."

      - name: cheaper-plan
        kind: tariff_plan
        query: sum(increase(home_energy_kwh_total[1h]))
        unit: kWh                # kWh per step, or W | kW averaged over each step
        window: 30d              # the usage to price; tier caps apply to its total
        step: 1h
        every: 1d                # at most daily (the default and the minimum)
        min_saving: 500          # minor units over the window
        recommendation:
          title: "{plan} would have cost {saving} {currency} less over {window}"
          action: Compare {plan} with {current_plan} with your provider before switching.

**Load shifting.** While the tariff's current period is one of ``periods``, and a period with a lower rate
starts within ``horizon``, each sample of ``query`` at or above ``min_shiftable`` is a candidate. Its
energy is the sample itself (kWh) or the power times ``duration`` (W, kW), and its saving is
``energy x (current rate - cheaper rate)`` in the currency's minor unit. The cheapest period ahead wins
(the earliest on a tie). A candidate saving less than ``min_saving`` is dropped. Candidates go through
the same ``for`` clock as threshold rules (kept in the store), and the dedup key is
``rule{labels_key...}`` as for threshold rules. Outside the watched periods, or with nothing cheaper
ahead, the rule is evaluated and clear (it doesn't query Prometheus). Period rates are used as they are:
block tiers on the active plan don't change the marginal saving.

**Plan comparison.** ``query`` is run as a range query over ``window`` at ``step``; the series are summed
point by point. Each point is the energy of the step that ends at it, and is priced at the period
containing that step's midpoint (local time, ``schedule.timezone``). A plan with ``tiers`` prices the
window's total through :func:`~argus_scout.tariff.tiered_cost` instead (so make ``window`` your billing
period). Every plan pays ``daily_charge`` for each day of data. If the cheapest plan isn't the active
one and saves at least ``min_saving`` over the window, it's recommended under the key
``rule{plan="<name>"}``, so a daily run refreshes the same recommendation rather than adding one. The
rule runs at most once per ``every`` (the last run time is kept in the store, so a restart doesn't
re-run it); between runs it reports nothing, which the engine reads as neither clear nor unknown. A
comparison plan whose periods leave gaps is left out of the comparison. Less data than
``min_coverage`` of the window makes the run unknown.

**Unknown and skipped.** A Prometheus error makes the rule unknown for the cycle (never clear). No tariff
loaded, a tariff whose periods leave gaps (:func:`~argus_scout.tariff.coverage_gaps`), a tariff not priced
per kWh, or a rule naming a period the tariff doesn't have, skips the rule with the reason recorded as
unknown: it can't tell, so it doesn't clear anything, and nothing crashes.

**Placeholders.** On top of the reserved ones (``value`` is the sample for a shift and the saving in minor
units for a plan; ``threshold`` is ``min_shiftable`` or ``min_saving``):

- shift: ``period``, ``rate``, ``cheaper_period``, ``cheaper_rate``, ``starts_at`` (local HH:MM),
  ``starts_in``, ``energy`` (kWh), ``saving`` (major units, e.g. ``1.25``), ``currency``, ``unit``, and the
  rule's ``labels_key`` and ``labels``;
- plan: ``plan``, ``current_plan``, ``plan_cost``, ``current_cost``, ``saving`` (all major units),
  ``usage`` (kWh), ``window``, ``days``, ``currency``.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo
from typing import Any, Literal

import structlog
from pydantic import Field, field_validator, model_validator

from argus_scout.prometheus import PrometheusClient, PrometheusError, Sample, Series
from argus_scout.severity import Severity
from argus_scout.signals import (
    RESERVED,
    Evaluation,
    RuleBase,
    SignalHit,
    SignalState,
    check_label_names,
    dedup_key,
    format_duration,
    parse_duration,
    render,
)
from argus_scout.tariff import Plan, Tariff, TariffPeriod, coverage_gaps, current_period, tiered_cost

log = structlog.get_logger()

EnergyUnit = Literal["W", "kW", "kWh"]
_KW_PER_UNIT = {"W": 0.001, "kW": 1.0}
ZERO_DECIMAL_CURRENCIES = frozenset({"CLP", "ISK", "JPY", "KRW", "PYG", "UGX", "VND"})
MAX_RANGE_POINTS = 11_000
"""Prometheus refuses a range query with more points per series than this."""
LAST_RUN = "last_run"
"""The key a plan rule keeps its last run time under, in :class:`SignalState`."""

SHIFT_FIELDS = frozenset(
    {
        "period",
        "rate",
        "cheaper_period",
        "cheaper_rate",
        "starts_at",
        "starts_in",
        "energy",
        "saving",
        "currency",
        "unit",
    }
)
PLAN_FIELDS = frozenset(
    {"plan", "current_plan", "plan_cost", "current_cost", "saving", "usage", "window", "days", "currency"}
)


# --- money and energy -----------------------------------------------------------------------------------------


def format_money(minor: float, currency: str = "") -> str:
    """An amount in the currency's minor unit, in major units: ``125`` -> ``"1.25"`` (``"125"`` for JPY)."""
    if currency.upper() in ZERO_DECIMAL_CURRENCIES:
        return f"{minor:.0f}"
    return f"{minor / 100:.2f}"


def energy_kwh(value: float, unit: EnergyUnit, duration: timedelta) -> float:
    """kWh from a sample: as it is for ``kWh``, else power (W, kW) sustained for ``duration``."""
    if unit == "kWh":
        return value
    return value * _KW_PER_UNIT[unit] * duration.total_seconds() / 3600


def shift_saving(kwh: float, now: TariffPeriod, later: TariffPeriod) -> float:
    """Minor units saved by using ``kwh`` in ``later`` rather than ``now``."""
    return kwh * (now.rate - later.rate)


def tariff_problem(tariff: Tariff | None, error: str | None = None) -> str | None:
    """Why tariff rules can't run against ``tariff``, or None if they can."""
    if tariff is None:
        return f"no tariff loaded ({error})" if error else "no tariff loaded"
    gaps = coverage_gaps(tariff.periods)
    if gaps:
        spans = ", ".join(f"{a:%H:%M}-{b:%H:%M}" for a, b in gaps)
        return f"tariff periods leave gaps: {spans}"
    if tariff.unit.lower() != "kwh":
        return f"tariff is priced per {tariff.unit!r}; tariff rules need kWh"
    return None


def upcoming_starts(tariff: Tariff, local_now: datetime, horizon: timedelta) -> list[tuple[TariffPeriod, datetime]]:
    """Periods (other than the current one) that start within ``horizon`` of ``local_now``, soonest first."""
    active = current_period(tariff, local_now)
    out = []
    for period in tariff.periods:
        if period.name == active.name or period.start == period.end:
            continue
        at = local_now.replace(hour=period.start.hour, minute=period.start.minute, second=0, microsecond=0)
        if at <= local_now:
            at += timedelta(days=1)
        if at.astimezone(UTC) - local_now.astimezone(UTC) <= horizon:
            out.append((period, at))
    return sorted(out, key=lambda pair: pair[1])


def cheaper_ahead(
    tariff: Tariff, local_now: datetime, horizon: timedelta
) -> tuple[TariffPeriod, TariffPeriod, datetime] | None:
    """``(current, cheaper, starts_at)``: the cheapest period starting within ``horizon`` whose rate is below
    the current one (the earliest on a tie), or None."""
    active = current_period(tariff, local_now)
    cheaper = [(p, at) for p, at in upcoming_starts(tariff, local_now, horizon) if p.rate < active.rate]
    if not cheaper:
        return None
    period, at = min(cheaper, key=lambda pair: (pair[0].rate, pair[1]))
    return active, period, at


@dataclass(frozen=True)
class PlanCost:
    plan: str
    energy: float
    standing: float

    @property
    def total(self) -> float:
        return self.energy + self.standing


def usage_points(series: Iterable[Series], step: timedelta, unit: EnergyUnit) -> list[tuple[datetime, float]]:
    """kWh per step, summed across series by timestamp (NaN and negative points dropped), oldest first."""
    total: dict[datetime, float] = {}
    for one in series:
        for at, value in one.points:
            if math.isfinite(value) and value >= 0:
                total[at] = total.get(at, 0.0) + energy_kwh(value, unit, step)
    return sorted(total.items())


def _period_at(plan: Plan, local: datetime) -> TariffPeriod:
    t = local.time()
    return next((p for p in plan.periods if p.contains_time(t)), max(plan.periods, key=lambda p: p.rate))


def plan_cost(plan: Plan, usage: list[tuple[datetime, float]], step: timedelta, tz: tzinfo, days: float) -> PlanCost:
    """What ``usage`` (from :func:`usage_points`) would cost on ``plan``, in minor units.

    Tiers, if the plan has them, price the total; otherwise each step is priced at the period holding its
    midpoint. ``days`` of ``daily_charge`` are added.
    """
    if plan.tiers:
        energy = tiered_cost(sum(units for _, units in usage), plan.tiers)
    else:
        energy = sum(units * _period_at(plan, (at - step / 2).astimezone(tz)).rate for at, units in usage)
    return PlanCost(plan.name, energy, days * plan.daily_charge)


def comparable_plans(tariff: Tariff) -> tuple[list[Plan], list[str]]:
    """The active plan and every comparison plan that can price any hour, and why any were left out."""
    plans, left_out = [tariff.plan], []
    for plan in tariff.comparison_plans:
        gaps = None if plan.tiers else coverage_gaps(plan.periods)
        if gaps:
            left_out.append(f"{plan.name}: periods leave gaps")
        else:
            plans.append(plan)
    return plans, left_out


# --- rules ----------------------------------------------------------------------------------------------------


def _positive(value: timedelta, name: str) -> timedelta:
    if value <= timedelta(0):
        raise ValueError(f"{name} must be more than 0")
    return value


class TariffShiftRule(RuleBase):
    """``kind: tariff_shift``: a load running in an expensive period with a cheaper one ahead."""

    kind: Literal["tariff_shift"]
    unit: EnergyUnit = "W"
    min_shiftable: float = Field(gt=0)
    duration: timedelta = timedelta(hours=1)
    periods: list[str] = Field(min_length=1)
    horizon: timedelta = timedelta(hours=12)
    min_saving: float = Field(default=0, ge=0)
    for_: timedelta = Field(default=timedelta(0), alias="for")
    severity: Severity = Severity.NOTICE
    labels_key: list[str] = Field(default_factory=lambda: ["instance"])
    labels: list[str] = Field(default_factory=list)

    @field_validator("duration", "horizon", "for_", mode="before")
    @classmethod
    def _durations(cls, value: Any) -> timedelta:
        return parse_duration(value)

    @field_validator("labels_key", "labels")
    @classmethod
    def _label_names(cls, value: list[str]) -> list[str]:
        return check_label_names(value)

    @field_validator("min_shiftable", "min_saving")
    @classmethod
    def _finite(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("must be a finite number")
        return value

    @model_validator(mode="after")
    def _check(self) -> TariffShiftRule:
        _positive(self.duration, "duration")
        if not timedelta(0) < self.horizon <= timedelta(days=1):
            raise ValueError("horizon must be more than 0 and at most 1d (periods repeat daily)")
        if len(set(self.periods)) != len(self.periods):
            raise ValueError("periods repeats a period")
        return self

    @property
    def placeholders(self) -> frozenset[str]:
        return RESERVED | SHIFT_FIELDS | set(self.labels_key) | set(self.labels)

    @property
    def summary(self) -> str:
        return (
            f"tariff_shift >= {self.min_shiftable:g} {self.unit} in {', '.join(self.periods)}, cheaper within "
            f"{format_duration(self.horizon)}, for {format_duration(self.for_)} -> {self.severity}"
        )


class TariffPlanRule(RuleBase):
    """``kind: tariff_plan``: price a window of real usage under every plan; recommend the cheapest."""

    kind: Literal["tariff_plan"]
    unit: EnergyUnit = "kWh"
    window: timedelta = timedelta(days=30)
    step: timedelta = timedelta(hours=1)
    every: timedelta = timedelta(days=1)
    min_saving: float = Field(default=0, ge=0)
    min_coverage: float = Field(default=0.9, gt=0, le=1)
    severity: Severity = Severity.INFO

    @field_validator("window", "step", "every", mode="before")
    @classmethod
    def _durations(cls, value: Any) -> timedelta:
        return parse_duration(value)

    @field_validator("min_saving")
    @classmethod
    def _finite(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("must be a finite number")
        return value

    @model_validator(mode="after")
    def _check(self) -> TariffPlanRule:
        if self.every < timedelta(days=1):
            raise ValueError("every must be at least 1d: a plan comparison is an expensive query")
        if not timedelta(days=1) <= self.window <= timedelta(days=400):
            raise ValueError("window must be between 1d and 400d")
        if not timedelta(minutes=1) <= self.step <= timedelta(days=1):
            raise ValueError("step must be between 1m and 1d")
        if self.window / self.step > MAX_RANGE_POINTS:
            raise ValueError(f"window / step is over {MAX_RANGE_POINTS} points; use a longer step")
        return self

    @property
    def placeholders(self) -> frozenset[str]:
        return RESERVED | PLAN_FIELDS

    @property
    def summary(self) -> str:
        return (
            f"tariff_plan over {format_duration(self.window)} every {format_duration(self.every)}, "
            f"saving >= {self.min_saving:g} -> {self.severity}"
        )


# --- evaluation -----------------------------------------------------------------------------------------------


def _hit(
    rule: TariffShiftRule | TariffPlanRule,
    key: str,
    labels: Mapping[str, str],
    value: float,
    threshold: float,
    since: datetime,
    fields: Mapping[str, Any],
) -> SignalHit:
    values: dict[str, Any] = dict(fields)
    values.update(value=value, threshold=threshold, op=">=", rule=rule.name, severity=rule.severity.value)
    values.setdefault("for", format_duration(getattr(rule, "for_", timedelta(0))))
    allowed = rule.placeholders
    tpl = rule.recommendation
    return SignalHit(
        rule=rule.name,
        key=key,
        labels=dict(labels),
        value=value,
        threshold=threshold,
        severity=rule.severity,
        since=since,
        title=render(tpl.title, values, allowed),
        action=render(tpl.action, values, allowed),
        rationale=render(tpl.rationale, values, allowed),
        impact=render(tpl.impact, values, allowed),
        confidence=tpl.confidence,
        rejection_ttl=rule.rejection_ttl,
        expires_after=rule.expires_after,
    )


def _skip(result: Evaluation, rule: RuleBase, reason: str) -> None:
    result.unknown[rule.name] = f"skipped: {reason}"
    log.info("tariff_rules.skipped", rule=rule.name, reason=reason)


async def _shift(
    rule: TariffShiftRule,
    client: PrometheusClient,
    tariff: Tariff,
    now: datetime,
    tz: tzinfo,
    state: SignalState,
    result: Evaluation,
) -> None:
    missing = sorted(set(rule.periods) - {p.name for p in tariff.periods})
    if missing:
        _skip(result, rule, f"the tariff has no period {', '.join(missing)}")
        return
    local = now.astimezone(tz)
    ahead = cheaper_ahead(tariff, local, rule.horizon) if current_period(tariff, local).name in rule.periods else None
    if ahead is None:  # not an expensive period, or nothing cheaper soon: known, and clear
        result.evaluated.add(rule.name)
        state.update(rule.name, {})
        return
    active, cheaper, starts_at = ahead
    try:
        samples = await client.query(rule.query, at=now)
    except PrometheusError as exc:
        result.unknown[rule.name] = str(exc)
        log.warning("signals.rule_unknown", rule=rule.name, error=str(exc))
        return
    result.evaluated.add(rule.name)
    crossing: dict[str, tuple[Sample, float, float]] = {}
    for sample in samples:
        if not math.isfinite(sample.value) or sample.value < rule.min_shiftable:
            continue
        kwh = energy_kwh(sample.value, rule.unit, rule.duration)
        saving = shift_saving(kwh, active, cheaper)
        if saving < rule.min_saving:
            continue
        key = dedup_key(rule.name, sample.labels, rule.labels_key)
        if key not in crossing or saving > crossing[key][2]:
            crossing[key] = (sample, kwh, saving)
    before = state.pending(rule.name)
    pending: dict[str, datetime] = {}
    starts_in = timedelta(minutes=round((starts_at.astimezone(UTC) - now.astimezone(UTC)).total_seconds() / 60))
    for key, (sample, kwh, saving) in crossing.items():
        since = before.get(key) or now
        pending[key] = since
        if now - since < rule.for_:
            result.pending += 1
            continue
        fields: dict[str, Any] = {name: sample.labels.get(name) for name in set(rule.labels_key) | set(rule.labels)}
        fields.update(
            period=active.name,
            rate=f"{active.rate:g}",
            cheaper_period=cheaper.name,
            cheaper_rate=f"{cheaper.rate:g}",
            starts_at=f"{starts_at:%H:%M}",
            starts_in=format_duration(starts_in),
            energy=f"{kwh:.2f}",
            saving=format_money(saving, tariff.currency),
            currency=tariff.currency,
            unit=rule.unit,
        )
        result.append(_hit(rule, key, sample.labels, sample.value, rule.min_shiftable, since, fields))
    state.update(rule.name, pending)


async def _plan(
    rule: TariffPlanRule,
    client: PrometheusClient,
    tariff: Tariff,
    now: datetime,
    tz: tzinfo,
    state: SignalState,
    result: Evaluation,
) -> None:
    last = state.pending(rule.name).get(LAST_RUN)
    if last is not None and now - last < rule.every:
        return  # not due: neither evaluated nor unknown, so the engine leaves its recommendation alone
    try:
        series = await client.query_range(rule.query, start=now - rule.window, end=now, step=rule.step)
    except PrometheusError as exc:
        result.unknown[rule.name] = str(exc)
        log.warning("signals.rule_unknown", rule=rule.name, error=str(exc))
        return
    state.update(rule.name, {LAST_RUN: now})
    usage = usage_points(series, rule.step, rule.unit)
    covered = len(usage) * rule.step
    coverage = covered / rule.window
    if coverage < rule.min_coverage:
        result.unknown[rule.name] = (
            f"not enough data: {coverage:.0%} of {format_duration(rule.window)} (needs {rule.min_coverage:.0%})"
        )
        log.info("tariff_rules.thin_data", rule=rule.name, coverage=round(coverage, 3))
        return
    days = covered / timedelta(days=1)
    plans, left_out = comparable_plans(tariff)
    for reason in left_out:
        log.info("tariff_rules.plan_left_out", rule=rule.name, reason=reason)
    costs = {plan.name: plan_cost(plan, usage, rule.step, tz, days) for plan in plans}
    result.evaluated.add(rule.name)
    current = costs[tariff.plan.name]
    best = min(costs.values(), key=lambda c: c.total)
    saving = current.total - best.total
    log.info(
        "tariff_rules.plans_compared",
        rule=rule.name,
        **{name: round(cost.total) for name, cost in costs.items()},
    )
    if best.plan == tariff.plan.name or saving <= 0 or saving < rule.min_saving:
        return
    labels = {"plan": best.plan}
    money = tariff.currency
    fields = {
        "plan": best.plan,
        "current_plan": tariff.plan.name,
        "plan_cost": format_money(best.total, money),
        "current_cost": format_money(current.total, money),
        "saving": format_money(saving, money),
        "usage": f"{sum(units for _, units in usage):.1f}",
        "window": format_duration(rule.window),
        "days": f"{days:.1f}",
        "currency": money,
    }
    key = dedup_key(rule.name, labels, ["plan"])
    result.append(_hit(rule, key, labels, saving, rule.min_saving, now, fields))


async def evaluate_tariff(
    rules: Iterable[RuleBase],
    client: PrometheusClient,
    tariff: Tariff | None,
    now: datetime,
    *,
    tz: tzinfo = UTC,
    state: SignalState | None = None,
    tariff_error: str | None = None,
) -> Evaluation:
    """Evaluate the tariff rules in ``rules`` (other kinds are ignored). See the module docstring.

    ``tz`` is the zone the tariff's wall-clock periods are in; ``tariff_error`` says why ``tariff`` is None,
    for the skip reason. Never raises for a missing or bad tariff, or a Prometheus outage.
    """
    state = state if state is not None else SignalState()
    result = Evaluation()
    mine = [r for r in rules if isinstance(r, TariffShiftRule | TariffPlanRule)]
    problem = tariff_problem(tariff, tariff_error) if mine else None
    for rule in mine:
        if problem or tariff is None:
            _skip(result, rule, problem or "no tariff loaded")
        elif isinstance(rule, TariffShiftRule):
            await _shift(rule, client, tariff, now, tz, state, result)
        else:
            await _plan(rule, client, tariff, now, tz, state, result)
    return result


__all__ = [
    "LAST_RUN",
    "PlanCost",
    "TariffPlanRule",
    "TariffShiftRule",
    "cheaper_ahead",
    "comparable_plans",
    "energy_kwh",
    "evaluate_tariff",
    "format_money",
    "plan_cost",
    "shift_saving",
    "tariff_problem",
    "upcoming_starts",
    "usage_points",
]
