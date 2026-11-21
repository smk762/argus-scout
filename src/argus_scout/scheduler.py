"""An asyncio scheduler for scout's own jobs: interval cycles and wall-clock briefings.

Jobs are plain async callables registered by name with a trigger:

- :class:`Every` runs a job every ``seconds`` (the analysis and status cycles).
- :class:`DailyAt` runs a job at ``HH:MM`` in a timezone (the briefings).

Guarantees, each covered by ``tests/test_scheduler.py``:

- **Deterministic.** The clock and the sleep are injected, so tests drive time by hand and never sleep.
- **Survives restarts.** Each job's last successful run is kept in the store (``job_runs``), so a restart
  neither repeats a briefing that already went out nor resets the interval cadence.
- **No catch-up storm.** A job has one "last run", not a queue: after downtime an interval job runs once,
  and a briefing runs only if its most recent slot passed less than ``grace`` ago. Older slots are skipped.
- **DST-safe.** Briefing slots are computed per local date in the configured zone, so a repeated hour fires
  once and a skipped hour (spring forward) fires at the first real instant after it.
- **Isolated.** A job that raises is logged and counted, and retried after ``retry_delay`` (still subject to
  its trigger); it never stops the scheduler or the other jobs. A job never overlaps a running copy of itself.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

import structlog

from argus_scout.store.base import JobRun, Store

log = structlog.get_logger()

Job = Callable[[], Awaitable[object]]
Clock = Callable[[], datetime]
Sleep = Callable[[float], Awaitable[object]]

DEFAULT_RETRY_DELAY = 60.0
MAX_SLEEP = 60.0
"""The loop never sleeps longer than this, so a wall-clock jump (suspend, NTP step) is noticed promptly."""
RUNNING_POLL = 1.0
"""While a job runs, the loop wakes this often, so the job's next slot (or retry) is computed soon after it ends."""


def utc_now() -> datetime:
    return datetime.now(UTC)


class Trigger(Protocol):
    def next_run(self, last_run: datetime | None, now: datetime) -> datetime:
        """When the job is next due, given the start of its last successful run. ``<= now`` means due now."""
        ...


@dataclass(frozen=True)
class Every:
    """Every ``seconds``, measured from the start of the last successful run. Runs at once if it never ran."""

    seconds: float

    def __post_init__(self) -> None:
        if self.seconds <= 0:
            raise ValueError("interval must be positive")

    def next_run(self, last_run: datetime | None, now: datetime) -> datetime:
        if last_run is None:
            return now
        # Overdue after downtime: last + interval is in the past, so it runs once now; the next
        # slot is then measured from that run, never a backlog.
        return last_run + timedelta(seconds=self.seconds)

    def describe(self) -> str:
        return f"every {self.seconds:g}s"


@dataclass(frozen=True)
class DailyAt:
    """At ``at`` local time in ``tz`` every day.

    A slot missed while scout was down runs late only if it passed less than ``grace`` ago; anything older is
    skipped, and only the most recent slot is ever considered, so a long outage never fires a burst.
    """

    at: time
    tz: ZoneInfo = field(default_factory=lambda: ZoneInfo("UTC"))
    grace: timedelta = timedelta(minutes=30)

    def slot(self, day: date) -> datetime:
        """The UTC instant of ``day``'s slot. A wall time skipped by DST maps forward past the gap (fold=0)."""
        local = datetime.combine(day, self.at, tzinfo=self.tz)
        return local.astimezone(UTC)

    def previous_slot(self, now: datetime) -> datetime:
        """The latest slot at or before ``now``."""
        day = now.astimezone(self.tz).date()
        slot = self.slot(day)
        return slot if slot <= now else self.slot(day - timedelta(days=1))

    def following_slot(self, now: datetime) -> datetime:
        """The first slot strictly after ``now``."""
        day = now.astimezone(self.tz).date()
        for offset in range(3):  # three days covers any DST shift
            slot = self.slot(day + timedelta(days=offset))
            if slot > now:
                return slot
        raise AssertionError("unreachable")  # pragma: no cover

    def next_run(self, last_run: datetime | None, now: datetime) -> datetime:
        prev = self.previous_slot(now)
        if (last_run is None or last_run < prev) and now - prev <= self.grace:
            return prev
        return self.following_slot(now)

    def describe(self) -> str:
        return f"daily at {self.at:%H:%M} {self.tz.key}"


@dataclass
class JobState:
    """A registered job and its live bookkeeping."""

    name: str
    func: Job
    trigger: Trigger
    retry_delay: float = DEFAULT_RETRY_DELAY
    last_run_at: datetime | None = None
    last_success_at: datetime | None = None
    last_error_at: datetime | None = None
    last_error: str | None = None
    run_count: int = 0
    error_count: int = 0
    running: bool = False
    failed_last: bool = False

    def due_at(self, now: datetime) -> datetime:
        due = self.trigger.next_run(self.last_run_at, now)
        if self.failed_last and self.last_error_at is not None:
            due = max(due, self.last_error_at + timedelta(seconds=self.retry_delay))
        return due

    def load(self, run: JobRun) -> None:
        self.last_run_at = run.last_run_at
        self.last_success_at = run.last_success_at
        self.last_error_at = run.last_error_at
        self.last_error = run.last_error
        self.run_count = run.run_count
        self.error_count = run.error_count
        # A failure newer than the last success means the retry delay still applies after a restart.
        self.failed_last = bool(
            run.last_error_at and (run.last_success_at is None or run.last_error_at > run.last_success_at)
        )


class Scheduler:
    """Runs named async jobs on their triggers. ``start()`` / ``stop()`` from the app's lifespan."""

    def __init__(
        self,
        store: Store | None = None,
        *,
        clock: Clock = utc_now,
        sleep: Sleep = asyncio.sleep,
        max_sleep: float = MAX_SLEEP,
    ) -> None:
        self.store = store
        self.clock = clock
        self.sleep = sleep
        self.max_sleep = max_sleep
        self.jobs: dict[str, JobState] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._loop_task: asyncio.Task[None] | None = None
        self._stopping = False

    # -- registration ----------------------------------------------------------
    def add(self, name: str, func: Job, trigger: Trigger, *, retry_delay: float = DEFAULT_RETRY_DELAY) -> JobState:
        """Register ``func`` under ``name``. Its persisted bookkeeping, if any, is loaded now."""
        if name in self.jobs:
            raise ValueError(f"job {name!r} is already registered")
        state = JobState(name=name, func=func, trigger=trigger, retry_delay=retry_delay)
        if self.store is not None:
            run = self.store.get_job_run(name)
            if run is not None:
                state.load(run)
        self.jobs[name] = state
        log.info("scheduler.job_added", job=name, trigger=getattr(trigger, "describe", lambda: repr(trigger))())
        return state

    # -- running -----------------------------------------------------------------
    def due(self, now: datetime | None = None) -> list[str]:
        """Names of jobs due at ``now`` that are not already running."""
        now = now or self.clock()
        return [name for name, job in self.jobs.items() if not job.running and job.due_at(now) <= now]

    async def tick(self, *, wait: bool = False) -> list[str]:
        """Start every due job (each in its own task) and return their names. ``wait`` awaits them all."""
        names = self.due(self.clock())
        for name in names:
            self._spawn(name)
        if wait:
            await self.drain()
        return names

    async def run_now(self, name: str) -> bool:
        """Run ``name`` now regardless of its trigger and wait for it. False if a copy is already running."""
        job = self.jobs[name]
        if job.running:
            log.info("scheduler.skip_overlap", job=name)
            return False
        await self._spawn(name)
        return True

    async def drain(self) -> None:
        """Wait for every running job to finish."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks.values()), return_exceptions=True)
            # Awaiting already-finished tasks doesn't yield, so the done callbacks may not have run yet.
            self._forget_done()

    def _spawn(self, name: str) -> asyncio.Task[None]:
        job = self.jobs[name]
        job.running = True  # set before the task starts, so a second tick can't start another copy
        task = asyncio.get_running_loop().create_task(self._run(job), name=f"scout-job-{name}")
        self._tasks[name] = task
        task.add_done_callback(lambda _t: self._forget_done())
        return task

    def _forget_done(self) -> None:
        for name, task in list(self._tasks.items()):
            if task.done():
                del self._tasks[name]

    async def _run(self, job: JobState) -> None:
        started = self.clock()
        error: str | None = None
        try:
            await job.func()
        except asyncio.CancelledError:
            job.running = False
            raise
        except Exception as exc:  # noqa: BLE001 — a failing job is counted and retried, never fatal
            error = f"{type(exc).__name__}: {exc}"
            log.exception("scheduler.job_failed", job=job.name)
        finished = self.clock()
        job.run_count += 1
        if error is None:
            job.last_run_at, job.last_success_at, job.failed_last = started, finished, False
        else:
            job.error_count += 1
            job.last_error_at, job.last_error, job.failed_last = finished, error, True
        if self.store is not None:
            try:
                self.store.record_job_run(job.name, started_at=started, finished_at=finished, error=error)
            except Exception:  # noqa: BLE001 — bookkeeping failure must not kill the job loop
                log.exception("scheduler.record_failed", job=job.name)
        job.running = False
        log.info("scheduler.job_ran", job=job.name, ok=error is None, seconds=(finished - started).total_seconds())

    def seconds_until_next(self, now: datetime | None = None) -> float:
        """How long the loop may sleep: until the earliest idle job is due, capped at ``max_sleep``."""
        now = now or self.clock()
        waits = [RUNNING_POLL if job.running else (job.due_at(now) - now).total_seconds() for job in self.jobs.values()]
        return max(0.0, min([self.max_sleep, *waits]))

    async def run_forever(self) -> None:
        """Tick, sleep until the next job is due, repeat, until :meth:`stop`."""
        self._stopping = False
        while not self._stopping:
            try:
                await self.tick()
                delay = self.seconds_until_next()
            except Exception:  # noqa: BLE001 — e.g. a broken trigger; keep the loop alive
                log.exception("scheduler.tick_failed")
                delay = self.max_sleep
            # Always yield, so jobs just spawned get to run even when the next one is due at once.
            await self.sleep(delay if delay > 0 else 0)

    def start(self) -> asyncio.Task[None]:
        """Start the loop as a background task on the running event loop."""
        if self._loop_task is None or self._loop_task.done():
            self._loop_task = asyncio.get_running_loop().create_task(self.run_forever(), name="scout-scheduler")
            log.info("scheduler.started", jobs=sorted(self.jobs))
        return self._loop_task

    def request_stop(self) -> None:
        """Ask :meth:`run_forever` to return after its current sleep. Running jobs are left to finish."""
        self._stopping = True

    async def stop(self, *, timeout: float = 10.0) -> None:
        """Stop the loop, give running jobs ``timeout`` seconds to finish, then cancel them."""
        self._stopping = True
        if self._loop_task is not None:
            self._loop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._loop_task
            self._loop_task = None
        running = list(self._tasks.values())
        if running:
            _done, pending = await asyncio.wait(running, timeout=timeout)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        log.info("scheduler.stopped")

    def snapshot(self) -> list[dict[str, object]]:
        now = self.clock()
        return [
            {
                "name": job.name,
                "running": job.running,
                "next_run": job.due_at(now).isoformat(),
                "last_success_at": job.last_success_at.isoformat() if job.last_success_at else None,
                "run_count": job.run_count,
                "error_count": job.error_count,
                "last_error": job.last_error,
            }
            for job in self.jobs.values()
        ]


__all__ = ["DailyAt", "Every", "Job", "JobState", "Scheduler", "Trigger", "utc_now"]
