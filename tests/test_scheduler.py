from __future__ import annotations

import asyncio
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from pydantic import BaseModel

from argus_scout.config import parse_config
from argus_scout.cycles import BriefingJob, PollJob, StatusCycle, register_jobs
from argus_scout.events import StatusEvent
from argus_scout.heartbeat import PolicyFile
from argus_scout.inbox import Inbox
from argus_scout.scheduler import DailyAt, Every, Scheduler
from argus_scout.sources import BoundSource, SourceError, Sources
from argus_scout.store import SqliteStore

LONDON = ZoneInfo("Europe/London")
T0 = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)


class FakeClock:
    """A clock the test moves by hand; ``sleep`` advances it instead of waiting."""

    def __init__(self, start: datetime) -> None:
        self.now = start
        self.horizon: datetime | None = None
        self.scheduler: Scheduler | None = None

    def __call__(self) -> datetime:
        return self.now

    def set(self, value: datetime) -> None:
        self.now = value

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(0)  # let jobs spawned by this tick run at the current time
        self.now += timedelta(seconds=seconds)
        if self.horizon is not None and self.now >= self.horizon and self.scheduler is not None:
            self.scheduler.request_stop()


def make_scheduler(clock: FakeClock, store=None, **kw) -> Scheduler:
    sched = Scheduler(store, clock=clock, sleep=clock.sleep, **kw)
    clock.scheduler = sched
    return sched


async def run_until(sched: Scheduler, clock: FakeClock, until: datetime) -> None:
    clock.horizon = until
    await asyncio.wait_for(sched.run_forever(), timeout=10)
    await sched.drain()


def recorder(clock: FakeClock, log: list[datetime]):
    async def job() -> None:
        log.append(clock())

    return job


@pytest.fixture
def store(tmp_path):
    with SqliteStore(tmp_path / "scout.db") as s:
        yield s


# -- interval jobs ------------------------------------------------------------------------


async def test_interval_cadence():
    clock = FakeClock(T0)
    sched = make_scheduler(clock)
    runs: list[datetime] = []
    sched.add("status", recorder(clock, runs), Every(300))
    await run_until(sched, clock, T0 + timedelta(seconds=1000))
    assert [(r - T0).total_seconds() for r in runs] == [0, 300, 600, 900]


async def test_interval_cadence_survives_restart(store):
    clock = FakeClock(T0)
    sched = make_scheduler(clock, store)
    runs: list[datetime] = []
    sched.add("analysis", recorder(clock, runs), Every(900))
    await sched.tick(wait=True)
    assert runs == [T0]

    clock.set(T0 + timedelta(seconds=60))  # restart a minute later
    restarted = make_scheduler(clock, store)
    restarted.add("analysis", recorder(clock, runs), Every(900))
    assert restarted.due() == []
    assert restarted.seconds_until_next() == 60  # capped by max_sleep; the job is 840s away
    assert restarted.jobs["analysis"].due_at(clock()) == T0 + timedelta(seconds=900)


async def test_interval_after_downtime_runs_once_then_resumes_cadence(store):
    clock = FakeClock(T0)
    sched = make_scheduler(clock, store)
    runs: list[datetime] = []
    sched.add("status", recorder(clock, runs), Every(300))
    await sched.tick(wait=True)

    down_until = T0 + timedelta(hours=6)  # 72 intervals missed
    clock.set(down_until)
    restarted = make_scheduler(clock, store)
    restarted.add("status", recorder(clock, runs), Every(300))
    await run_until(restarted, clock, down_until + timedelta(seconds=650))
    assert runs == [T0, down_until, down_until + timedelta(seconds=300), down_until + timedelta(seconds=600)]


# -- wall-clock jobs --------------------------------------------------------------------------


def local(y, mo, d, h, mi, tz=LONDON) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=tz).astimezone(UTC)


async def test_briefing_fires_at_local_time_across_dst():
    # Europe/London springs forward on 2026-03-29: 07:30 is 07:30 UTC before, 06:30 UTC after.
    clock = FakeClock(local(2026, 3, 27, 12, 0))
    sched = make_scheduler(clock, max_sleep=3600)
    runs: list[datetime] = []
    sched.add("briefing:morning", recorder(clock, runs), DailyAt(time(7, 30), LONDON))
    await run_until(sched, clock, local(2026, 3, 31, 12, 0))
    assert [r.astimezone(LONDON).strftime("%m-%d %H:%M") for r in runs] == [
        "03-28 07:30",
        "03-29 07:30",
        "03-30 07:30",
        "03-31 07:30",
    ]
    assert runs[0].hour == 7 and runs[1].hour == 6  # same wall time, an hour apart in UTC


async def test_briefing_in_repeated_hour_fires_once():
    # Clocks go back at 02:00 BST on 2026-10-25, so 01:30 happens twice that night.
    clock = FakeClock(local(2026, 10, 24, 12, 0))
    sched = make_scheduler(clock, max_sleep=600)
    runs: list[datetime] = []
    sched.add("briefing:night", recorder(clock, runs), DailyAt(time(1, 30), LONDON))
    await run_until(sched, clock, local(2026, 10, 26, 12, 0))
    days = [r.astimezone(LONDON).date().isoformat() for r in runs]
    assert days == ["2026-10-25", "2026-10-26"]


async def test_briefing_in_skipped_hour_fires_once_after_the_gap():
    # 01:30 doesn't exist on 2026-03-29 in London (01:00 GMT jumps to 02:00 BST).
    clock = FakeClock(local(2026, 3, 28, 12, 0))
    sched = make_scheduler(clock, max_sleep=600)
    runs: list[datetime] = []
    sched.add("briefing:night", recorder(clock, runs), DailyAt(time(1, 30), LONDON))
    await run_until(sched, clock, local(2026, 3, 30, 12, 0))
    assert len(runs) == 2
    assert runs[0] == datetime(2026, 3, 29, 1, 30, tzinfo=UTC)  # 02:30 BST, just past the gap


async def test_no_double_fire_after_restart(store):
    slot = local(2026, 6, 1, 7, 30)
    clock = FakeClock(slot + timedelta(seconds=5))
    sched = make_scheduler(clock, store)
    runs: list[datetime] = []
    sched.add("briefing:morning", recorder(clock, runs), DailyAt(time(7, 30), LONDON))
    assert await sched.tick(wait=True) == ["briefing:morning"]

    clock.set(slot + timedelta(minutes=2))  # crash and restart inside the grace window
    restarted = make_scheduler(clock, store)
    restarted.add("briefing:morning", recorder(clock, runs), DailyAt(time(7, 30), LONDON))
    assert restarted.due() == []
    assert restarted.jobs["briefing:morning"].due_at(clock()) == local(2026, 6, 2, 7, 30)
    assert len(runs) == 1


async def test_missed_briefing_runs_late_within_grace(store):
    slot = local(2026, 6, 1, 7, 30)
    clock = FakeClock(slot + timedelta(minutes=10))  # scout was down at 07:30
    sched = make_scheduler(clock, store)
    runs: list[datetime] = []
    sched.add("briefing:morning", recorder(clock, runs), DailyAt(time(7, 30), LONDON))
    assert await sched.tick(wait=True) == ["briefing:morning"]
    assert await sched.tick(wait=True) == []


async def test_no_catch_up_storm(store):
    trigger = DailyAt(time(7, 30), LONDON)
    clock = FakeClock(local(2026, 6, 1, 7, 30))
    sched = make_scheduler(clock, store)
    runs: list[datetime] = []
    sched.add("briefing:morning", recorder(clock, runs), trigger)
    sched.add("briefing:evening", recorder(clock, runs), DailyAt(time(19, 0), LONDON))
    await sched.tick(wait=True)
    assert len(runs) == 1

    # Down for three and a half days: six briefings missed. None replay on restart.
    clock.set(local(2026, 6, 4, 21, 0))
    restarted = make_scheduler(clock, store, max_sleep=3600)
    restarted.add("briefing:morning", recorder(clock, runs), trigger)
    restarted.add("briefing:evening", recorder(clock, runs), DailyAt(time(19, 0), LONDON))
    assert restarted.due() == []
    await run_until(restarted, clock, local(2026, 6, 5, 12, 0))
    assert [r.astimezone(LONDON).strftime("%d %H:%M") for r in runs[1:]] == ["05 07:30"]


# -- isolation --------------------------------------------------------------------------------


async def test_failing_job_is_isolated_counted_and_retried(store):
    clock = FakeClock(T0)
    sched = make_scheduler(clock, store)
    good: list[datetime] = []
    attempts: list[datetime] = []

    async def broken() -> None:
        attempts.append(clock())
        raise RuntimeError("boom")

    sched.add("broken", broken, Every(300), retry_delay=60)
    sched.add("good", recorder(clock, good), Every(100))
    await run_until(sched, clock, T0 + timedelta(seconds=250))

    assert len(good) == 3  # 0, 100, 200: the other job and the loop carry on
    assert [(a - T0).total_seconds() for a in attempts] == [0, 60, 120, 180, 240]
    job = sched.jobs["broken"]
    assert (job.run_count, job.error_count, job.last_success_at) == (5, 5, None)
    assert job.last_error == "RuntimeError: boom"
    persisted = store.get_job_run("broken")
    assert (persisted.run_count, persisted.error_count, persisted.last_run_at) == (5, 5, None)


async def test_job_recovers_after_failure(store):
    clock = FakeClock(T0)
    sched = make_scheduler(clock, store)
    calls = 0

    async def flaky() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ValueError("first time")

    sched.add("flaky", flaky, Every(300), retry_delay=30)
    await run_until(sched, clock, T0 + timedelta(seconds=400))
    job = sched.jobs["flaky"]
    assert (job.run_count, job.error_count) == (3, 1)  # fail at 0, succeed at 30, then 330
    assert job.last_run_at == T0 + timedelta(seconds=330)


async def test_job_never_overlaps_itself():
    clock = FakeClock(T0)
    sched = make_scheduler(clock)
    release = asyncio.Event()
    started = 0

    async def slow() -> None:
        nonlocal started
        started += 1
        await release.wait()

    sched.add("slow", slow, Every(1))
    assert await sched.tick() == ["slow"]
    await asyncio.sleep(0)
    clock.set(T0 + timedelta(seconds=10))  # long overdue, but still running
    assert await sched.tick() == []
    assert await sched.run_now("slow") is False
    assert started == 1
    release.set()
    await sched.drain()
    assert await sched.tick() == ["slow"]
    release.set()
    await sched.drain()
    assert started == 2


async def test_start_and_stop_with_real_loop():
    sched = Scheduler()
    ran = asyncio.Event()

    async def job() -> None:
        ran.set()

    sched.add("once", job, Every(3600))
    sched.start()
    await asyncio.wait_for(ran.wait(), timeout=2)
    await sched.stop()
    assert sched.jobs["once"].run_count == 1


def test_duplicate_job_name_refused():
    sched = Scheduler()

    async def job() -> None: ...

    sched.add("x", job, Every(1))
    with pytest.raises(ValueError):
        sched.add("x", job, Every(1))


# -- the cycles -------------------------------------------------------------------------------


def _event(severity="warning") -> StatusEvent:
    return StatusEvent.from_dict(
        {"event_id": f"e-{severity}", "source": "nas", "severity": severity, "summary": "Disk filling"}
    )


async def test_bad_heartbeat_file_keeps_status_cycle_alive(store, write_file):
    path = write_file("heartbeat.yaml", "report: {min_severity: warning}\n")
    store.upsert_event(_event("notice"), now=T0)
    clock = FakeClock(T0)
    cycle = StatusCycle(store, PolicyFile(path), clock=clock)
    result = await cycle()
    assert result.policy.file_loaded and result.policy.report_min_severity == "warning"
    assert result.reportable == []

    write_file("heartbeat.yaml", "report: [this is: not: valid\n")
    sched = make_scheduler(clock, store)
    sched.add("status", cycle, Every(300))
    await run_until(sched, clock, T0 + timedelta(seconds=650))
    job = sched.jobs["status"]
    assert (job.run_count, job.error_count) == (3, 0)
    assert cycle.last.policy.file_error  # fell back to defaults...
    assert cycle.last.policy.report_min_severity == "notice"
    assert len(cycle.last.reportable) == 1  # ...and kept deciding with them


async def test_status_cycle_survives_policy_loader_raising(store, monkeypatch):
    cycle = StatusCycle(store, PolicyFile(None), clock=FakeClock(T0))

    def explode():
        raise OSError("disk gone")

    monkeypatch.setattr(cycle.policy_file, "get", explode)
    result = await cycle()
    assert result.policy.file_error == "disk gone"


async def test_status_cycle_reads_the_inbox_grouped_and_rests(store, write_file):
    path = write_file("heartbeat.yaml", "rest_hours: {start: '00:30', end: '06:00'}\n")
    clock = FakeClock(local(2026, 6, 1, 3, 0))
    inbox = Inbox(store, clock=clock)
    # Two ids saying the same thing collapse into one group; a third is its own.
    for event_id in ("a", "b"):
        inbox.ingest({"event_id": event_id, "source": "nas", "severity": "critical", "summary": "Disk filling"})
    inbox.ingest({"event_id": "c", "source": "nas", "severity": "warning", "summary": "Fan slow"})
    cycle = StatusCycle(store, PolicyFile(path), inbox=inbox, tz=LONDON, clock=clock)
    rested = await cycle()
    assert rested.resting and rested.groups == []
    clock.set(local(2026, 6, 1, 9, 0))
    result = await cycle()
    assert not result.resting
    assert [g.count for g in result.groups] == [2, 1]
    assert len(result.reportable) == 2


# -- poll jobs ---------------------------------------------------------------------------------


class FakeConfig(BaseModel):
    name: str
    interval: float = 60.0


class FakePoller:
    kind = "fake"
    config_model = FakeConfig

    def __init__(self, replies: dict[str, list]) -> None:
        self.replies = replies
        self.calls: list[str] = []

    async def poll(self, cfg: FakeConfig) -> list[StatusEvent]:
        self.calls.append(cfg.name)
        reply = self.replies[cfg.name]
        if isinstance(reply, Exception):
            raise reply
        return [
            StatusEvent.from_dict({"source": cfg.name, "severity": "warning", **item})
            if isinstance(item, dict)
            else item
            for item in reply
        ]


async def test_poll_jobs_ingest_and_one_failing_source_is_isolated(store):
    plugin = FakePoller(
        {
            "alerts": [{"event_id": "disk", "summary": "Disk 91% full"}],
            "down": SourceError("down.lan answered 503"),
        }
    )
    sources = Sources(
        sources=[
            BoundSource(plugin, FakeConfig(name="alerts", interval=30)),
            BoundSource(plugin, FakeConfig(name="down", interval=30)),
        ]
    )
    clock = FakeClock(T0)
    sched = make_scheduler(clock, store)
    register_jobs(sched, parse_config({}), store, PolicyFile(None), inbox=Inbox(store, clock=clock), sources=sources)
    assert {"poll:alerts", "poll:down"} <= set(sched.jobs)
    for name in ("status", "analysis"):
        del sched.jobs[name]
    await run_until(sched, clock, T0 + timedelta(seconds=70))

    alerts, down = sched.jobs["poll:alerts"], sched.jobs["poll:down"]
    assert (alerts.run_count, alerts.error_count) == (3, 0)  # 0, 30, 60
    assert alerts.func.outcomes == {"new": 1, "refreshed": 2}
    assert (down.run_count, down.error_count) == (3, 3)
    assert "503" in down.last_error
    assert [e.event_id for e in store.open_events(now=clock())] == ["disk"]


async def test_poll_job_skips_a_refused_event_and_keeps_the_rest(store):
    bad = StatusEvent.from_dict({"source": "alerts", "event_id": "x", "summary": "ok"})
    bad.summary = ""  # the inbox refuses an event that says nothing
    plugin = FakePoller({"alerts": [bad, {"event_id": "good", "summary": "Fine"}]})
    job = PollJob(BoundSource(plugin, FakeConfig(name="alerts")), Inbox(store))
    await job()
    assert job.outcomes["refused"] == 1 and job.outcomes["new"] == 1


async def test_register_jobs_from_config(store):
    config = parse_config(
        {
            "schedule": {
                "status_interval": 120,
                "analysis_interval": 600,
                "timezone": "Europe/London",
                "briefings": [{"kind": "morning", "at": "07:30"}, {"kind": "evening", "at": "19:00"}],
            }
        }
    )
    clock = FakeClock(local(2026, 6, 1, 7, 29))
    sched = make_scheduler(clock, store)
    register_jobs(sched, config, store, PolicyFile(None), sources=Sources())
    assert set(sched.jobs) == {"status", "analysis", "briefing:morning", "briefing:evening"}
    assert sorted(await sched.tick(wait=True)) == ["analysis", "status"]
    clock.set(local(2026, 6, 1, 7, 30))
    assert await sched.tick(wait=True) == ["briefing:morning"]
    briefing = sched.jobs["briefing:morning"].func
    assert isinstance(briefing, BriefingJob) and briefing.runs == [clock()]
