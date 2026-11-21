"""``/metrics``: scout's own state in Prometheus exposition format.

One custom collector reads everything at scrape time (the store, the usage caps, the tariff file), so the
values are whatever is true now and survive restarts: nothing is accumulated in process memory. It is
registered on its own :class:`~prometheus_client.CollectorRegistry`, never the global one, so two apps in
one process (tests) don't collide.

Families (all prefixed ``scout_``):

- ``usage_used{kind}``, ``usage_limit{kind}`` (gauges): units used today and the daily cap; 0 means unlimited.
- ``tariff_loaded`` (gauge): 1 if a tariff file is configured and loads, else 0.
- ``tariff_period{plan,period}`` (gauge): 1 for the active period.
- ``tariff_rate{plan,period,currency,unit}`` (gauge): the active rate, in the currency's minor unit.
- ``tariff_next_change_seconds{next_period}`` (gauge): seconds until the next period starts.
- ``recommendations{status}`` (gauge): recommendations in each lifecycle status.
- ``approvals_total{source}`` (counter): approvals in the audit trail, ``manual`` or ``standing``.
- ``inbox_open_events{severity}`` (gauge): unacked, unexpired status events.
- ``job_runs_total{job}``, ``job_errors_total{job}`` (counters): scheduler runs, and runs that raised.
- ``job_last_success_timestamp_seconds{job}`` (gauge): Unix time the job last finished cleanly.
- ``job_running{job}`` (gauge): 1 while the job is running.
"""

from __future__ import annotations

from collections.abc import Iterator
from zoneinfo import ZoneInfo

import structlog
from prometheus_client import CollectorRegistry
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily, Metric
from prometheus_client.registry import Collector

from argus_scout.budget import DailyBudget
from argus_scout.scheduler import Clock, Scheduler, utc_now
from argus_scout.store.base import Store
from argus_scout.tariff import TariffFile, current_period, next_period_change

log = structlog.get_logger()

PREFIX = "scout"


class ScoutCollector(Collector):
    """Reads scout's state at scrape time. Each part is optional; a part that fails is logged and skipped."""

    def __init__(
        self,
        *,
        store: Store | None = None,
        budget: DailyBudget | None = None,
        tariff: TariffFile | None = None,
        scheduler: Scheduler | None = None,
        tz: ZoneInfo | None = None,
        clock: Clock = utc_now,
    ) -> None:
        self.store = store
        self.budget = budget
        self.tariff = tariff
        self.scheduler = scheduler
        self.tz = tz or ZoneInfo("UTC")
        self.clock = clock

    def collect(self) -> Iterator[Metric]:
        for part in (self._usage, self._tariff, self._store, self._jobs):
            try:
                yield from part()
            except Exception:  # noqa: BLE001 — one broken source must not blank the whole scrape
                log.exception("metrics.collect_failed", part=part.__name__)

    def _usage(self) -> Iterator[Metric]:
        used = GaugeMetricFamily(f"{PREFIX}_usage_used", "Units used today against a daily cap.", labels=["kind"])
        limit = GaugeMetricFamily(f"{PREFIX}_usage_limit", "Daily cap per kind; 0 means unlimited.", labels=["kind"])
        if self.budget is not None:
            for kind, row in self.budget.snapshot()["kinds"].items():
                used.add_metric([kind], row["used"])
                limit.add_metric([kind], row["limit"])
        yield used
        yield limit

    def _tariff(self) -> Iterator[Metric]:
        tariff = self.tariff.get() if self.tariff is not None else None
        yield GaugeMetricFamily(
            f"{PREFIX}_tariff_loaded", "1 if a tariff is configured and loads, else 0.", value=int(tariff is not None)
        )
        period = GaugeMetricFamily(
            f"{PREFIX}_tariff_period", "1 for the active tariff period.", labels=["plan", "period"]
        )
        rate = GaugeMetricFamily(
            f"{PREFIX}_tariff_rate",
            "Active tariff rate, in the currency's minor unit per unit.",
            labels=["plan", "period", "currency", "unit"],
        )
        change = GaugeMetricFamily(
            f"{PREFIX}_tariff_next_change_seconds", "Seconds until the next tariff period.", labels=["next_period"]
        )
        if tariff is not None:
            # Tariff periods are local wall-clock times: evaluate them in the schedule's zone.
            local = self.clock().astimezone(self.tz).replace(tzinfo=None)
            active = current_period(tariff, local)
            following, seconds = next_period_change(tariff, local)
            period.add_metric([tariff.plan.name, active.name], 1)
            rate.add_metric([tariff.plan.name, active.name, tariff.currency, tariff.unit], active.rate)
            change.add_metric([following], seconds)
        yield period
        yield rate
        yield change

    def _store(self) -> Iterator[Metric]:
        recs = GaugeMetricFamily(f"{PREFIX}_recommendations", "Recommendations by status.", labels=["status"])
        approvals = CounterMetricFamily(
            f"{PREFIX}_approvals", "Approvals in the audit trail, by source.", labels=["source"]
        )
        inbox = GaugeMetricFamily(
            f"{PREFIX}_inbox_open_events", "Open (unacked, unexpired) status events.", labels=["severity"]
        )
        if self.store is not None:
            for status, n in self.store.recommendation_counts().items():
                recs.add_metric([status.value], n)
            for source, n in self.store.approval_counts().items():
                approvals.add_metric([source], n)
            for severity, n in self.store.open_event_counts(now=self.clock()).items():
                inbox.add_metric([severity], n)
        yield recs
        yield approvals
        yield inbox

    def _jobs(self) -> Iterator[Metric]:
        runs = CounterMetricFamily(f"{PREFIX}_job_runs", "Scheduler job runs.", labels=["job"])
        errors = CounterMetricFamily(f"{PREFIX}_job_errors", "Scheduler job runs that raised.", labels=["job"])
        success = GaugeMetricFamily(
            f"{PREFIX}_job_last_success_timestamp_seconds", "Unix time a job last finished cleanly.", labels=["job"]
        )
        running = GaugeMetricFamily(f"{PREFIX}_job_running", "1 while a job is running.", labels=["job"])
        # The store is the source of truth (it survives restarts); the scheduler adds jobs that never ran yet.
        rows: dict[str, tuple[int, int, float | None]] = {}
        if self.scheduler is not None:
            for job in self.scheduler.jobs.values():
                ts = job.last_success_at.timestamp() if job.last_success_at else None
                rows[job.name] = (job.run_count, job.error_count, ts)
                running.add_metric([job.name], int(job.running))
        if self.store is not None:
            for run in self.store.list_job_runs():
                ts = run.last_success_at.timestamp() if run.last_success_at else None
                rows[run.name] = (run.run_count, run.error_count, ts)
        for name, (n_runs, n_errors, ts) in sorted(rows.items()):
            runs.add_metric([name], n_runs)
            errors.add_metric([name], n_errors)
            if ts is not None:
                success.add_metric([name], ts)
        yield runs
        yield errors
        yield success
        yield running


def build_registry(collector: ScoutCollector) -> CollectorRegistry:
    """A fresh registry holding only scout's collector."""
    registry = CollectorRegistry(auto_describe=False)
    registry.register(collector)
    return registry


__all__ = ["ScoutCollector", "build_registry"]
