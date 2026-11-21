"""The redelivery sweep: failed report deliveries tried again, bounded, idempotent, and never spoken late."""

from __future__ import annotations

import functools
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from argus_cortex.net import afetch

from argus_scout.channels import (
    BoundChannel,
    ChannelError,
    Delivery,
    DeliveryResult,
    Dispatcher,
    OpenGates,
    RedeliveryJob,
    SpeechAudio,
)
from argus_scout.channels.webhook import WebhookChannel
from argus_scout.config import DeliveryConfig, TextChannel, parse_config
from argus_scout.config import WebhookChannel as WebhookChannelConfig
from argus_scout.cycles import register_jobs
from argus_scout.heartbeat import PolicyFile
from argus_scout.scheduler import Scheduler
from argus_scout.sources import Sources
from argus_scout.store import SqliteStore

T0 = datetime(2026, 11, 21, 7, 30, tzinfo=UTC)


class Clock:
    def __init__(self, now=T0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, **kw):
        self.now += timedelta(**kw)


class Cfg:
    def __init__(self, name, kinds=()):
        self.name, self.enabled, self.min_severity, self.kinds, self.timeout = name, True, "info", list(kinds), 5.0


class Flaky:
    """A text channel that fails ``fails`` times (``permanent`` refusals if asked), then succeeds."""

    kind = "fake"
    config_model = TextChannel

    def __init__(self, fails=0, permanent=False):
        self.fails, self.permanent, self.calls = fails, permanent, 0

    async def deliver(self, message):
        self.calls += 1
        if self.calls <= self.fails:
            raise ChannelError("receiver down", permanent=self.permanent)
        return DeliveryResult(ok=True)


class Speaker(Flaky):
    def __init__(self):
        super().__init__()
        self.played = []

    async def play(self, audio, message):
        self.played.append(audio)
        return DeliveryResult(ok=True)


class Synth:
    def __init__(self, fails=0):
        self.fails, self.calls = fails, 0

    async def synthesise(self, text, message):
        self.calls += 1
        if self.calls <= self.fails:
            raise RuntimeError("echo down")
        return SpeechAudio(data=b"\0\0", format="pcm_s16le", text=text)


@pytest.fixture
def store():
    s = SqliteStore(":memory:")
    yield s
    s.close()


def setup(store, channels, *, synth=None, clock=None, **delivery):
    clock = clock or Clock()
    bound = [BoundChannel(ch, Cfg(name, *extra)) for name, ch, *extra in channels]
    disp = Dispatcher(bound, synthesiser=synth or Synth(), gates=OpenGates(), store=store, clock=clock)
    job = RedeliveryJob(store, disp, DeliveryConfig(**delivery), clock=clock)
    return disp, job, clock


def first(store, clock, kind="morning"):
    return store.add_report(kind, clock(), "All quiet.")


def ok_rows(store, key):
    return [r for r in store.list_deliveries(key) if r.ok]


async def test_retries_a_failed_channel_until_it_succeeds_and_stops_after_max_attempts(store):
    log, hook = Flaky(), Flaky(fails=10)
    disp, job, clock = setup(store, [("log", log), ("hook", hook)], max_attempts=3)
    report = first(store, clock)
    await disp.deliver(Delivery.from_report(report))
    assert (log.calls, hook.calls) == (1, 1)
    for _ in range(4):
        clock.advance(minutes=5)
        await job()
    # Attempt 1 plus two retries: three failures, then it gives up. The text channel that worked is never resent.
    assert (log.calls, hook.calls) == (1, 3)
    assert len([r for r in store.list_deliveries("report:1") if r.channel == "hook" and not r.ok]) == 3


async def test_a_retry_that_succeeds_writes_one_ok_row_and_is_idempotent(store):
    hook = Flaky(fails=1)
    disp, job, clock = setup(store, [("hook", hook)])
    report = first(store, clock)
    await disp.deliver(Delivery.from_report(report))
    clock.advance(minutes=5)
    result = await job()
    assert result.sent == [("report:1", "hook")] and hook.calls == 2
    for _ in range(3):  # later sweeps, and the original path delivering again, send nothing more
        clock.advance(minutes=5)
        assert (await job()).reports == []
    again = await disp.deliver(Delivery.from_report(report))
    assert again.get("hook").status == "duplicate"
    assert hook.calls == 2 and len(ok_rows(store, "report:1")) == 1


async def test_no_stale_speech(store):
    """Voice failed (echo down): retried while the report is fresh, text only once it is older than speak_max_age."""
    log, speaker = Flaky(fails=1), Speaker()
    synth = Synth(fails=2)
    disp, job, clock = setup(store, [("log", log), ("speaker", speaker)], synth=synth, speak_max_age=600)
    report = first(store, clock)
    await disp.deliver(Delivery.from_report(report))  # text fails, synthesis fails
    clock.advance(minutes=5)
    await job()  # fresh: both retried; text now works, synthesis fails again
    assert (log.calls, synth.calls, speaker.played) == (2, 2, [])
    clock.advance(minutes=6)  # 11 minutes old
    result = await job()
    assert result.stale_voice == [("report:1", "speaker")] and result.reports == []
    assert synth.calls == 2 and speaker.played == []  # never synthesised, never spoken late
    assert ok_rows(store, "report:1")[0].channel == "log"


async def test_speak_max_age_zero_never_respeaks(store):
    speaker = Speaker()
    disp, job, clock = setup(store, [("speaker", speaker)], synth=Synth(fails=1), speak_max_age=0)
    await disp.deliver(Delivery.from_report(first(store, clock)))
    result = await job()
    assert result.stale_voice == [("report:1", "speaker")] and speaker.played == []


async def test_permanent_failures_are_not_retried(store):
    refused = Flaky(fails=1, permanent=True)
    disp, job, clock = setup(store, [("refused", refused)])
    await disp.deliver(Delivery.from_report(first(store, clock)))
    [row] = store.list_deliveries("report:1")
    assert row.permanent and not row.ok
    clock.advance(minutes=5)
    assert (await job()).reports == [] and refused.calls == 1


async def test_a_webhook_4xx_is_permanent_and_a_429_is_not(store):
    def channel(status):
        fetcher = functools.partial(
            afetch,
            transport=httpx.MockTransport(lambda request: httpx.Response(status)),
            resolve=lambda host, port: ["10.0.0.7"],
        )
        cfg = WebhookChannelConfig(kind="webhook", name=f"hook{status}", url="http://hooks.lan:8080/x", retries=0)
        return (cfg.name, WebhookChannel(cfg, fetcher=fetcher))

    disp, job, clock = setup(store, [channel(404), channel(429), channel(503)])
    await disp.deliver(Delivery.from_report(first(store, clock)))
    permanent = {r.channel: r.permanent for r in store.list_deliveries("report:1")}
    assert permanent == {"hook404": True, "hook429": False, "hook503": False}
    clock.advance(minutes=5)
    result = await job()
    assert sorted(result.failed) == [("report:1", "hook429"), ("report:1", "hook503")]


async def test_too_old_and_never_tried_channels_are_left_alone(store):
    hook, morning_only = Flaky(fails=10), Flaky()
    disp, job, clock = setup(store, [("hook", hook), ("morning", morning_only, ["morning"])], retry_max_age=3600)
    report = first(store, clock, kind="evening")  # filtered from "morning": never tried, so never backfilled
    await disp.deliver(Delivery.from_report(report))
    clock.advance(minutes=30)
    assert [c for _, c in (await job()).failed] == ["hook"]
    clock.advance(minutes=31)  # past retry_max_age
    assert (await job()).reports == []
    assert (hook.calls, morning_only.calls) == (2, 0)


async def test_register_jobs_adds_the_sweep_with_a_dispatcher(store):
    config = parse_config({"delivery": {"retry_interval": 120}})
    clock = Clock()
    plain = Scheduler(store, clock=clock)
    register_jobs(plain, config, store, PolicyFile(None), sources=Sources())
    assert "redeliver" not in plain.jobs
    sched = Scheduler(store, clock=clock)
    register_jobs(sched, config, store, PolicyFile(None), sources=Sources(), dispatcher=Dispatcher([]))
    assert sched.jobs["redeliver"].trigger.seconds == 120


def test_delivery_config_defaults():
    cfg = parse_config({}).delivery
    assert (cfg.retry_interval, cfg.retry_max_age, cfg.max_attempts, cfg.speak_max_age) == (300, 21600, 5, 600)
    assert parse_config({"delivery": None}).delivery == cfg
