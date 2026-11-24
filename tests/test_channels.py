from __future__ import annotations

import asyncio
import functools
import hashlib
import hmac
import json
import sys
import textwrap
from datetime import UTC, datetime, timedelta
from importlib.metadata import EntryPoint
from pathlib import Path

import httpx
import pytest
from argus_cortex.net import afetch
from typer.testing import CliRunner

from argus_scout.channels import (
    ENTRY_POINT_GROUP,
    BoundChannel,
    ChannelError,
    Delivery,
    DeliveryResult,
    Dispatcher,
    HeartbeatGates,
    NullSynthesiser,
    OpenGates,
    SpeechAudio,
    VoiceChannel,
    build_channels,
    build_dispatcher,
    load_registry,
)
from argus_scout.channels import registry as registry_module
from argus_scout.channels.text import TextChannel, append, clean, target_path
from argus_scout.channels.webhook import PAYLOAD_SCHEMA, WebhookChannel, build_payload, post, sign
from argus_scout.cli import app
from argus_scout.config import ConfigError, parse_config
from argus_scout.config import TextChannel as TextChannelConfig
from argus_scout.config import WebhookChannel as WebhookChannelConfig
from argus_scout.events import StatusEvent
from argus_scout.heartbeat import PolicyFile
from argus_scout.server.app import build_runtime
from argus_scout.severity import Severity
from argus_scout.store import Report, SqliteStore

T0 = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)
runner = CliRunner()


def report(id_: int = 1, kind: str = "morning", text: str = "All quiet.", **meta) -> Report:
    return Report(id=id_, kind=kind, generated_at=T0, text=text, meta=meta)


def event(**kw) -> StatusEvent:
    raw = {"source": "nas", "event_id": "disk", "severity": "warning", "summary": "Disk 90% full", **kw}
    return StatusEvent.from_dict(raw)


# --- fakes ---------------------------------------------------------------------------------------------------


class Cfg:
    def __init__(self, name, **kw):
        self.name = name
        self.enabled = kw.get("enabled", True)
        self.min_severity = kw.get("min_severity", "info")
        self.kinds = kw.get("kinds", [])
        self.timeout = kw.get("timeout", 5.0)


class Recorder:
    kind = "fake"
    config_model = TextChannelConfig

    def __init__(self, behaviour="ok"):
        self.behaviour = behaviour
        self.got: list[Delivery] = []

    async def deliver(self, message):
        self.got.append(message)
        if self.behaviour == "raise":
            raise ChannelError("receiver is down")
        if self.behaviour == "hang":
            await asyncio.sleep(10)
        if self.behaviour == "false":
            return DeliveryResult(ok=False, error="said no")
        return DeliveryResult(ok=True, detail="ok")


class Speaker(Recorder):
    kind = "speaker"

    def __init__(self, behaviour="ok"):
        super().__init__(behaviour)
        self.played: list[SpeechAudio] = []

    async def play(self, audio, message):
        self.played.append(audio)
        if self.behaviour == "raise":
            raise RuntimeError("speaker unplugged")
        return DeliveryResult(ok=True, detail="played")


class FakeSynth:
    def __init__(self, fail=False):
        self.calls: list[str] = []
        self.fail = fail

    async def synthesise(self, text, message):
        self.calls.append(text)
        if self.fail:
            raise RuntimeError("tts down")
        return SpeechAudio(data=b"RIFF....", text=text)


def bound(channel, name, **kw) -> BoundChannel:
    return BoundChannel(channel, Cfg(name, **kw))


# --- deliveries ---------------------------------------------------------------------------------------------


def test_delivery_from_report_and_event():
    d = Delivery.from_report(report(7, severity="notice", speakable="Short version."))
    assert (d.type, d.kind, d.key, d.severity, d.speakable) == (
        "report",
        "morning",
        "report:7",
        Severity.NOTICE,
        "Short version.",
    )
    assert Delivery.from_report(report(speakable=False)).speakable == ""
    assert Delivery.from_report(report()).speakable == "All quiet."

    e = event(body_md="Pool tank at 90%", speakable=False)
    d = Delivery.from_event(e)
    assert d.type == "event" and d.kind == "event" and d.speakable == ""
    assert "Pool tank" in d.text and d.key.startswith("event:nas/disk:")
    # A re-announcement is a new message; the same announcement is not.
    assert Delivery.from_event(e).key == d.key
    e.broadcast_count = 1
    assert Delivery.from_event(e).key != d.key


# --- fan-out -------------------------------------------------------------------------------------------------


async def test_fan_out_and_a_failing_channel_never_blocks_the_others():
    good, bad, slow, liar = Recorder(), Recorder("raise"), Recorder("hang"), Recorder("false")
    store = SqliteStore(":memory:")
    dispatcher = Dispatcher(
        [bound(bad, "bad"), bound(slow, "slow", timeout=0.05), bound(good, "good"), bound(liar, "liar")],
        store=store,
        gates=OpenGates(),
    )
    result = await dispatcher.deliver(Delivery.from_report(report()))
    assert result.sent == ["good"]
    assert sorted(result.failed) == ["bad", "liar", "slow"]
    assert "receiver is down" in result.get("bad").error
    assert "timed out" in result.get("slow").error
    assert result.get("liar").error == "said no"
    assert len(good.got) == 1
    assert dispatcher.stats[("good", "sent")] == 1 and dispatcher.stats[("bad", "failed")] == 1
    assert store.delivered_channels("report:1") == {"good"}
    assert {(r.channel, r.ok) for r in store.list_deliveries("report:1")} == {
        ("good", True),
        ("bad", False),
        ("slow", False),
        ("liar", False),
    }
    assert store.delivery_counts()[("bad", False)] == 1
    # Failed channels are retried on the next delivery; the one that got it is not.
    again = await dispatcher.deliver(Delivery.from_report(report()))
    assert again.get("good").status == "duplicate"
    assert len(bad.got) == 2


async def test_channels_run_concurrently():
    class Slow(Recorder):
        async def deliver(self, message):
            await asyncio.sleep(0.2)
            return DeliveryResult(ok=True)

    dispatcher = Dispatcher([bound(Slow(), f"c{i}") for i in range(5)], gates=OpenGates())
    loop = asyncio.get_running_loop()
    start = loop.time()
    result = await dispatcher.deliver(Delivery.from_report(report()))
    assert len(result.sent) == 5
    assert loop.time() - start < 0.6


async def test_speech_is_synthesised_once_for_every_voice_channel():
    synth = FakeSynth()
    speakers = [Speaker() for _ in range(3)]
    text = Recorder()
    dispatcher = Dispatcher(
        [bound(text, "log"), *(bound(s, f"room{i}") for i, s in enumerate(speakers))],
        synthesiser=synth,
        gates=OpenGates(),
    )
    assert all(isinstance(s, VoiceChannel) for s in speakers) and not isinstance(text, VoiceChannel)
    result = await dispatcher.deliver(Delivery.from_report(report(speakable="Good morning.")))
    assert synth.calls == ["Good morning."]
    assert dispatcher.syntheses == 1 and result.synthesised
    audio = speakers[0].played[0]
    assert all(s.played == [audio] for s in speakers)  # the very same audio object
    assert all(s.got == [] for s in speakers)  # voice channels get audio, not deliver()
    assert sorted(result.sent) == ["log", "room0", "room1", "room2"]


async def test_a_failing_speaker_or_synthesiser_fails_voice_only():
    text, ok_speaker, broken = Recorder(), Speaker(), Speaker("raise")
    channels = [bound(text, "log"), bound(ok_speaker, "kitchen"), bound(broken, "garage")]
    result = await Dispatcher(channels, synthesiser=FakeSynth(), gates=OpenGates()).deliver(
        Delivery.from_report(report())
    )
    assert sorted(result.sent) == ["kitchen", "log"] and result.failed == ["garage"]

    result = await Dispatcher(channels, synthesiser=FakeSynth(fail=True), gates=OpenGates()).deliver(
        Delivery.from_report(report(2))
    )
    assert result.sent == ["log"]
    assert sorted(result.failed) == ["garage", "kitchen"]
    assert "speech synthesis failed" in result.get("kitchen").error


async def test_no_synthesiser_holds_voice_back_and_text_still_goes():
    text, speaker = Recorder(), Speaker()
    dispatcher = Dispatcher(
        [bound(text, "log"), bound(speaker, "kitchen")], synthesiser=NullSynthesiser(), gates=OpenGates()
    )
    result = await dispatcher.deliver(Delivery.from_report(report()))
    assert result.sent == ["log"]
    assert result.get("kitchen").status == "suppressed" and result.get("kitchen").detail == "no speech synthesiser"


async def test_the_voice_gate_suppresses_voice_but_not_text(write_file):
    policy = write_file("heartbeat.yaml", "quiet_hours: {start: '22:00', end: '07:00'}\n")
    synth, text, speaker = FakeSynth(), Recorder(), Speaker()
    night = T0.replace(hour=23)
    dispatcher = Dispatcher(
        [bound(text, "log"), bound(speaker, "kitchen")],
        synthesiser=synth,
        gates=HeartbeatGates(PolicyFile(policy)),
        clock=lambda: night,
    )
    result = await dispatcher.deliver(Delivery.from_report(report()))
    assert result.sent == ["log"]
    assert result.get("kitchen").status == "suppressed"
    assert result.voice_rule.startswith("quiet_hours.min_severity")
    assert synth.calls == []  # nothing synthesised for a message nobody will hear

    # Critical still speaks in quiet hours; the gate is the heartbeat policy's, not a copy of it.
    result = await dispatcher.deliver(Delivery.from_report(report(2, severity="critical")))
    assert sorted(result.sent) == ["kitchen", "log"]
    assert result.voice_rule.startswith("quiet_hours.exempt:critical")


def test_heartbeat_gates(write_file):
    gates = HeartbeatGates(PolicyFile(write_file("hb.yaml", "rest_hours: {start: '01:00', end: '06:00'}\n")))
    assert gates.voice(Delivery.from_report(report()), T0) == (True, "open; sources.default.channel=voice")
    assert gates.voice(Delivery.from_report(report()), T0.replace(hour=2)) == (False, "rest_hours")
    # Inbox items go through voice_gate: below voice.min_severity (warning) is not spoken.
    assert gates.voice(Delivery.from_event(event(severity="notice")), T0)[0] is False
    assert gates.voice(Delivery.from_event(event(severity="warning")), T0)[0] is True
    # Routing reports to text keeps them off the speakers.
    gates = HeartbeatGates(PolicyFile(write_file("hb2.yaml", "sources: {scout: {channel: text}}\n")))
    assert gates.voice(Delivery.from_report(report()), T0) == (False, "sources.scout.channel=text")
    # A bad policy file means the defaults, never an exception.
    gates = HeartbeatGates(PolicyFile(write_file("hb3.yaml", "quiet_hours: [nope\n")))
    assert gates.voice(Delivery.from_report(report()), T0)[0] is True


async def test_severity_kind_and_enabled_filters():
    chans = {
        "all": Recorder(),
        "loud": Recorder(),
        "mornings": Recorder(),
        "events": Recorder(),
        "off": Recorder(),
    }
    dispatcher = Dispatcher(
        [
            bound(chans["all"], "all"),
            bound(chans["loud"], "loud", min_severity="warning"),
            bound(chans["mornings"], "mornings", kinds=["morning"]),
            bound(chans["events"], "events", kinds=["event"]),
            bound(chans["off"], "off", enabled=False),
        ],
        gates=OpenGates(),
    )
    result = await dispatcher.deliver(Delivery.from_report(report(severity="notice")))
    assert sorted(result.sent) == ["all", "mornings"]
    assert result.get("loud").status == "filtered" and "below min_severity=warning" in result.get("loud").detail
    assert result.get("events").status == "filtered"
    assert result.get("off").status == "disabled"

    result = await dispatcher.deliver(Delivery.from_event(event(severity="critical")))
    assert sorted(result.sent) == ["all", "events", "loud"]

    result = await dispatcher.deliver(Delivery.from_report(report(9, kind="evening")), only=["mornings", "all"])
    assert result.sent == ["all"] and [r.channel for r in result.results] == ["all", "mornings"]


async def test_no_duplicate_delivery_after_restart(tmp_path):
    db = tmp_path / "scout.db"
    first = Recorder()
    with SqliteStore(db) as store:
        result = await Dispatcher([bound(first, "log")], store=store, gates=OpenGates()).deliver(
            Delivery.from_report(report(3))
        )
        assert result.sent == ["log"]

    second = Recorder()
    with SqliteStore(db) as store:
        dispatcher = Dispatcher([bound(second, "log"), bound(Recorder(), "new")], store=store, gates=OpenGates())
        result = await dispatcher.deliver(Delivery.from_report(report(3)))
        assert result.get("log").status == "duplicate"
        assert result.sent == ["new"]  # a channel added since still gets it
        assert second.got == []
        forced = await dispatcher.deliver(Delivery.from_report(report(3)), force=True)
        assert sorted(forced.sent) == ["log", "new"] and len(second.got) == 1
        # Still one successful row per (message, channel).
        assert [r.channel for r in store.list_deliveries("report:3") if r.ok].count("log") == 1


async def test_concurrent_deliveries_of_one_message_send_once():
    class Slow(Recorder):
        async def deliver(self, message):
            self.got.append(message)
            await asyncio.sleep(0.05)
            return DeliveryResult(ok=True)

    slow = Slow()
    dispatcher = Dispatcher([bound(slow, "log")], store=SqliteStore(":memory:"), gates=OpenGates())
    a, b = await asyncio.gather(*(dispatcher.deliver(Delivery.from_report(report())) for _ in range(2)))
    assert sorted([a.get("log").status, b.get("log").status]) == ["duplicate", "sent"]
    assert len(slow.got) == 1


# --- text channel --------------------------------------------------------------------------------------------


def text_cfg(tmp_path, **kw) -> TextChannelConfig:
    return TextChannelConfig.model_validate({"kind": "text", "name": "log", "dir": str(tmp_path / "out"), **kw})


async def test_text_channel_appends(tmp_path):
    cfg = text_cfg(tmp_path, file="{kind}/{date}.log")
    channel = TextChannel(cfg)
    result = await channel.deliver(Delivery.from_report(report(text="Line one\x1b[31m red‮")))
    assert result.ok
    path = tmp_path / "out" / "morning" / "2026-11-21.log"
    body = path.read_text()
    assert "report:1" in body and "Line one" in body
    assert "\x1b" not in body and "‮" not in body
    await channel.deliver(Delivery.from_report(report(2, text="Second")))
    assert path.read_text().count("===") == 4


def test_text_channel_paths_outside_root_are_refused(tmp_path):
    for bad in ("../escape.log", "/etc/passwd", "a/../../x.log", "{nope}.log"):
        with pytest.raises(ValueError):
            text_cfg(tmp_path, file=bad)
    with pytest.raises(ConfigError, match="channels.0.text.file"):
        parse_config({"channels": [{"kind": "text", "name": "log", "file": "../x.log"}]})

    root = tmp_path / "out"
    root.mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (root / "link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ChannelError, match="outside its root"):
        append(text_cfg(tmp_path, file="link/x.log"), Delivery.from_report(report()))
    (root / "victim.log").symlink_to(outside / "victim.log")
    with pytest.raises(ChannelError, match="outside its root"):
        append(text_cfg(tmp_path, file="victim.log"), Delivery.from_report(report()))
    assert list(outside.iterdir()) == []

    # A kind is reduced to safe characters before it is filled in.
    cfg = text_cfg(tmp_path, file="{kind}.log")
    path = target_path(cfg, Delivery.from_report(report(kind="../../etc/cron.d/x")))
    assert path.parent == root.resolve() and path.name == "etc_cron.d_x.log"


def test_text_channel_rotates(tmp_path):
    cfg = text_cfg(tmp_path, rotate_bytes=200, keep=2)
    for i in range(6):
        append(cfg, Delivery.from_report(report(i, text="x" * 80)))
    root = tmp_path / "out"
    names = sorted(p.name for p in root.iterdir())
    assert names == ["scout-reports.log", "scout-reports.log.1", "scout-reports.log.2"]
    assert all(p.stat().st_size <= 200 for p in root.iterdir())


# --- webhook channel -----------------------------------------------------------------------------------------

HOOK = WebhookChannelConfig.model_validate(
    {
        "kind": "webhook",
        "name": "chat",
        "url": "http://hooks.lan:8080/scout",
        "token_env": "HOOK_TOKEN",
        "secret_env": "HOOK_SECRET",
        "retries": 2,
        "backoff": 0.5,
    }
)
ENV = {"HOOK_TOKEN": "tok", "HOOK_SECRET": "shh"}


def stub_fetcher(*statuses):
    """The real afetch over a mock transport and a stubbed resolver: no network."""
    seen: list[httpx.Request] = []
    replies = list(statuses)

    def handler(request):
        seen.append(request)
        status = replies.pop(0) if replies else 200
        if status == "drop":
            raise httpx.ConnectError("refused")
        return httpx.Response(status, text="ok")

    fetcher = functools.partial(afetch, transport=httpx.MockTransport(handler), resolve=lambda host, port: ["10.0.0.7"])
    return fetcher, seen


async def test_webhook_payload_and_signature():
    fetcher, seen = stub_fetcher(200)
    message = Delivery.from_event(event(link="https://ui.example/x"))
    result = await post(HOOK, message, environ=ENV, fetcher=fetcher, now=T0)
    assert result.ok
    [request] = seen
    assert request.method == "POST" and request.headers["host"] == "hooks.lan:8080"
    assert request.headers["authorization"] == "Bearer tok"
    assert request.headers["content-type"] == "application/json"
    assert request.headers["x-scout-delivery"] == message.key
    body = request.content
    payload = json.loads(body)
    assert payload == build_payload(message, channel="chat", sent_at=T0)
    assert payload["schema"] == PAYLOAD_SCHEMA and payload["schema_version"] == 1
    assert payload["type"] == "event" and payload["severity"] == "warning"
    assert payload["event"] == {
        "source": "nas",
        "event_id": "disk",
        "requires_ack": False,
        "link": "https://ui.example/x",
    }
    timestamp = request.headers["x-scout-timestamp"]
    assert timestamp == str(int(T0.timestamp()))
    expected = "sha256=" + hmac.new(b"shh", timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    assert request.headers["x-scout-signature"] == expected == sign("shh", timestamp, body)


async def test_webhook_retries_with_backoff_then_gives_up():
    sleeps: list[float] = []

    async def sleep(seconds):
        sleeps.append(seconds)

    fetcher, seen = stub_fetcher(503, "drop", 200)
    result = await post(HOOK, Delivery.from_report(report()), environ=ENV, fetcher=fetcher, sleep=sleep)
    assert result.ok and "after 3 attempts" in result.detail
    assert sleeps == [0.5, 1.0] and len(seen) == 3

    sleeps.clear()
    fetcher, seen = stub_fetcher(500, 502, 503, 504)
    with pytest.raises(ChannelError, match=r"answered 503 \(3 attempts\)"):
        await post(HOOK, Delivery.from_report(report()), environ=ENV, fetcher=fetcher, sleep=sleep)
    assert len(seen) == 3

    fetcher, seen = stub_fetcher(400)
    with pytest.raises(ChannelError, match=r"answered 400 \(1 attempt\)"):
        await post(HOOK, Delivery.from_report(report()), environ=ENV, fetcher=fetcher, sleep=sleep)
    assert len(seen) == 1


async def test_webhook_refuses_to_send_without_its_secrets_or_to_another_host():
    fetcher, seen = stub_fetcher(200)
    with pytest.raises(ChannelError, match="HOOK_SECRET is not set"):
        await post(HOOK, Delivery.from_report(report()), environ={"HOOK_TOKEN": "tok"}, fetcher=fetcher)
    assert seen == []
    # A redirect is never followed for a POST.
    fetcher, seen = stub_fetcher(302)
    with pytest.raises(ChannelError, match="answered 302"):
        await WebhookChannel(HOOK, environ=ENV, fetcher=fetcher).deliver(Delivery.from_report(report()))


# --- registry and config --------------------------------------------------------------------------------------

PLUGIN_MODULE = """
from typing import Literal

from pydantic import field_validator

from argus_scout.channels import DeliveryResult
from argus_scout.config import ChannelSettings


class BellConfig(ChannelSettings):
    kind: Literal["bell"]
    room: str


class Bell:
    kind = "bell"
    config_model = BellConfig
    rung = []

    def __init__(self, config):
        self.config = config

    async def deliver(self, message):
        return DeliveryResult(ok=True, detail="text")

    async def play(self, audio, message):
        Bell.rung.append((self.config.room, audio.text))
        return DeliveryResult(ok=True, detail=f"rang in {self.config.room}")


class GrumpyConfig(ChannelSettings):
    kind: Literal["grumpy"]


class Grumpy(Bell):
    kind = "grumpy"
    config_model = GrumpyConfig

    def __init__(self, config):
        raise RuntimeError("no device")


class NotAChannel:
    pass


class TouchyConfig(ChannelSettings):
    kind: Literal["touchy"]
    level: str = "ok"

    @field_validator("level")
    @classmethod
    def _check(cls, value):
        return {"ok": value}[value]  # KeyError: pydantic doesn't wrap it in a ValidationError


class Touchy(Bell):
    kind = "touchy"
    config_model = TouchyConfig
"""


@pytest.fixture
def plugin_module(tmp_path, monkeypatch):
    (tmp_path / "fake_scout_channel.py").write_text(textwrap.dedent(PLUGIN_MODULE))
    monkeypatch.syspath_prepend(str(tmp_path))
    yield "fake_scout_channel"
    sys.modules.pop("fake_scout_channel", None)


def ep(name: str, value: str) -> EntryPoint:
    return EntryPoint(name=name, value=value, group=ENTRY_POINT_GROUP)


def test_builtins_are_registered_through_entry_points():
    registry = load_registry()
    assert {"text", "webhook"} <= set(registry.kinds)
    assert registry.errors == {}


async def test_third_party_voice_channel_from_an_entry_point(plugin_module, monkeypatch, tmp_path):
    monkeypatch.setattr(
        registry_module,
        "entry_points",
        lambda group: [ep("bell", f"{plugin_module}:Bell")] if group == ENTRY_POINT_GROUP else [],
    )
    config = parse_config(
        {
            "channels": [
                {"kind": "bell", "name": "hall", "room": "hall"},
                {"kind": "bell", "name": "attic", "room": "attic"},
                {"kind": "text", "name": "log", "dir": str(tmp_path)},
            ]
        },
        tmp_path,
    )
    channels, dispatcher = build_dispatcher(config, synthesiser=FakeSynth())
    assert channels.errors == {}
    hall = channels.get("hall")
    assert hall.kind == "bell" and hall.voice and type(hall.config).__name__ == "BellConfig"
    assert not channels.get("log").voice
    result = await dispatcher.deliver(Delivery.from_report(report(severity="warning")))
    assert sorted(result.sent) == ["attic", "hall", "log"]
    assert sorted(sys.modules[plugin_module].Bell.rung) == [("attic", "All quiet."), ("hall", "All quiet.")]
    assert dispatcher.syntheses == 1


def test_bad_channel_entries_do_not_stop_the_others(plugin_module, tmp_path):
    registry = load_registry(
        [
            ep("bell", f"{plugin_module}:Bell"),
            ep("grumpy", f"{plugin_module}:Grumpy"),
            ep("broken", "no_such_module_anywhere:Thing"),
            ep("junk", f"{plugin_module}:NotAChannel"),
            ep("text", f"{plugin_module}:Bell"),  # can't take over a built-in kind
        ]
    )
    assert "failed to load" in registry.errors["broken"]
    assert "not a channel plugin" in registry.errors["junk"]
    assert "built-in kind" in registry.errors["text"]
    config = parse_config(
        {
            "channels": [
                {"kind": "bell", "name": "typo", "room": "x", "volume": 11},
                {"kind": "bell", "name": "missing"},
                {"kind": "grumpy", "name": "grump"},
                {"kind": "broken", "name": "b"},
                {"kind": "carrier_pigeon", "name": "c"},
                {"kind": "bell", "name": "good", "room": "hall"},
                {"kind": "text", "name": "log"},
            ]
        },
        tmp_path,
    )
    built = build_channels(config, registry)
    assert [c.name for c in built.channels] == ["good", "log"]
    assert "unknown key" in built.errors["typo"]
    assert "room" in built.errors["missing"]
    assert "could not start" in built.errors["grump"]
    assert "did not load" in built.errors["b"]
    assert "unknown channel kind" in built.errors["c"]


def test_a_plugin_model_raising_anything_fails_only_its_channel(plugin_module, tmp_path):
    registry = load_registry([ep("touchy", f"{plugin_module}:Touchy")])
    config = parse_config(
        {"channels": [{"kind": "touchy", "name": "t", "level": "cross"}, {"kind": "touchy", "name": "fine"}]},
        tmp_path,
    )
    built = build_channels(config, registry)
    assert [c.name for c in built.channels] == ["fine"]
    assert built.errors["t"] == "touchy: KeyError: 'cross'"


def test_channel_kind_that_is_not_text_is_a_config_error():
    for kind in (["text"], {"x": 1}):
        with pytest.raises(ConfigError, match="channels.0.plugin.kind: Input should be a valid string"):
            parse_config({"channels": [{"kind": kind, "name": "a"}]})
    assert parse_config({"channels": [{"kind": "text", "name": "a", "kinds": None}]}).channels[0].kinds == []


def test_channel_config():
    with pytest.raises(ConfigError, match="duplicate channel name"):
        parse_config({"channels": [{"kind": "text", "name": "a"}, {"kind": "text", "name": "a"}]})
    with pytest.raises(ConfigError, match="min_severity"):
        parse_config({"channels": [{"kind": "text", "name": "a", "min_severity": "loud"}]})
    with pytest.raises(ConfigError, match="not an http"):
        parse_config({"channels": [{"kind": "webhook", "name": "w", "url": "ftp://x"}]})
    config = parse_config(
        {
            "channels": [
                {"kind": "webhook", "name": "w", "url": "https://h.example", "token_env": "A", "secret_env": "B"}
            ]
        }
    )
    assert config.secret_env_names() == ["SCOUT_API_TOKEN", "A", "B"]
    assert parse_config({"channels": None}).channels == []


# --- wiring --------------------------------------------------------------------------------------------------


async def test_runtime_deliver_writes_the_text_channel(tmp_path):
    config = parse_config({"channels": [{"kind": "text", "name": "log"}], "store": {"path": ":memory:"}}, tmp_path)
    store = SqliteStore(":memory:")
    runtime = build_runtime(config, store=store)
    stored = store.add_report("morning", T0, "Morning, all quiet.")
    result = await runtime.deliver(stored)
    assert result.sent == ["log"]
    assert "Morning, all quiet." in (tmp_path / "scout-reports.log").read_text()
    assert (await runtime.deliver(stored)).get("log").status == "duplicate"


def test_cli_deliver_latest(tmp_path):
    path = tmp_path / "scout.yaml"
    path.write_text("channels:\n  - kind: text\n    name: log\n")
    assert runner.invoke(app, ["deliver", "--config", str(path), "--latest"]).exit_code == 1  # no report yet
    with SqliteStore(tmp_path / "scout.db") as store:
        store.add_report("morning", T0, "First")
        store.add_report("evening", T0 + timedelta(hours=8), "Second")
    result = runner.invoke(app, ["deliver", "--config", str(path), "--latest", "--kind", "morning"])
    assert result.exit_code == 0, result.output
    assert "log: sent" in result.output
    result = runner.invoke(app, ["deliver", "--config", str(path), "--latest", "--kind", "morning"])
    assert "log: duplicate" in result.output and result.exit_code == 0
    result = runner.invoke(app, ["deliver", "--config", str(path), "--latest", "--force", "--channel", "log"])
    assert "log: sent" in result.output
    log = (tmp_path / "scout-reports.log").read_text()
    assert log.count("First") == 1 and log.count("Second") == 1
    result = runner.invoke(app, ["deliver", "--config", str(path), "--latest", "--channel", "nope"])
    assert result.exit_code == 1 and "no channel named nope" in result.output
    assert runner.invoke(app, ["deliver", "--config", str(path)]).exit_code == 2


def test_cli_deliver_charges_the_speech_caps(tmp_path, monkeypatch):
    """`deliver` builds its dispatcher with the store-backed daily caps, so speech it sends counts against tts/speak
    and stops at the cap, the same as the server's."""
    import argus_scout.cli as cli_module

    path = tmp_path / "scout.yaml"
    path.write_text("channels:\n  - kind: text\n    name: log\nbudget:\n  limits:\n    tts: 1\n")
    with SqliteStore(tmp_path / "scout.db") as store:
        store.add_report("status", T0, "UPS on battery", {"severity": "critical"})
        today = datetime.now(UTC).date().isoformat()
        assert store.consume_usage(today, "tts", 1) == (True, 1)  # one charge against a limit of 1: spent
        assert store.usage_counts(today) == {"tts": 1}
    seen = {}
    real = cli_module.build_dispatcher

    def spy(config, **kw):
        channels, dispatcher = real(config, **kw)
        seen["budget"] = dispatcher.budget
        seen["tts_available"] = dispatcher.budget is not None and dispatcher.budget.available("tts")
        return channels, dispatcher

    monkeypatch.setattr(cli_module, "build_dispatcher", spy)
    result = runner.invoke(app, ["deliver", "--config", str(path), "--latest"])
    assert result.exit_code == 0, result.output
    budget = seen["budget"]
    assert budget is not None and budget.limit_for("tts") == 1
    assert seen["tts_available"] is False  # counted in the store: the earlier charge is seen


def test_check_config_reports_a_bad_channel(tmp_path):
    path = tmp_path / "scout.yaml"
    path.write_text("channels:\n  - kind: text\n    name: log\n  - kind: smoke_signal\n    name: hill\n")
    result = runner.invoke(app, ["check", "config", str(path)])
    assert result.exit_code == 1
    assert "channel hill: error: unknown channel kind 'smoke_signal'" in result.output
    assert "2 channels" in result.output


def test_starter_config_delivers_text_out_of_the_box(tmp_path):
    from importlib.resources import files as package_files

    path = tmp_path / "scout.yaml"
    path.write_text((package_files("argus_scout") / "examples" / "scout.yaml").read_text())
    from argus_scout.config import load_config

    config = load_config(path, load_env=False)
    channels = build_channels(config)
    assert [(c.name, c.kind) for c in channels.channels] == [("log", "text")] and channels.errors == {}
    assert Path(channels.get("log").config.dir) == tmp_path


async def test_a_briefing_job_writes_its_report_to_the_text_channel_once(tmp_path):
    config = parse_config(
        {
            "channels": [{"kind": "text", "name": "log"}],
            "schedule": {"briefings": [{"kind": "morning", "at": "07:30"}]},
        },
        tmp_path,
    )
    store = SqliteStore(":memory:")
    runtime = build_runtime(config, store=store, clock=lambda: T0)
    assert await runtime.scheduler.run_now("briefing:morning@07:30")
    [stored] = store.list_reports(kind="morning")
    log = (tmp_path / "scout-reports.log").read_text()
    assert log.count(f"report:{stored.id}") == 1
    assert stored.text.strip().splitlines()[0] in log
    # Delivering it again (a retry, the CLI, a restart) doesn't write it twice.
    assert (await runtime.deliver(stored)).get("log").status == "duplicate"
    assert (tmp_path / "scout-reports.log").read_text() == log
    assert store.delivered_channels(f"report:{stored.id}") == {"log"}


def test_plain_speech():
    from argus_scout.channels.base import plain_speech

    md = "# Morning\n\n- **Disk** is _90%_ full, see [ui](http://x)\n```\ncode\n```\n1. Two `cmd`"
    assert plain_speech(md) == "Morning Disk is 90% full, see ui Two cmd"
    assert Delivery.from_report(report(text="**Hi** there")).speakable == "Hi there"


# --- disabled channels ---------------------------------------------------------------------------------------

DISABLED_VOICE = [
    {"kind": "snapcast", "name": "house", "enabled": False, "fifo": "/run/snapcast/scout.fifo"},
    {
        "kind": "mask",
        "name": "face",
        "enabled": False,
        "url": "http://mask:8000",
        "face": "f",
        "transport": "websocket",
    },
    {"kind": "text", "name": "log"},
]


def _without_extras(monkeypatch):
    for name in ("numpy", "websockets", "websockets.asyncio", "websockets.asyncio.client"):
        monkeypatch.setitem(sys.modules, name, None)  # importing it now raises ImportError


def test_a_disabled_channel_needs_no_extra_and_is_never_built(tmp_path, monkeypatch):
    _without_extras(monkeypatch)
    config = parse_config({"channels": DISABLED_VOICE}, tmp_path)
    built = build_channels(config)
    assert built.errors == {}
    assert built.disabled == ["house", "face"]
    assert [c.name for c in built.channels] == ["log"]  # the dispatcher only ever sees enabled channels
    _, dispatcher = build_dispatcher(config, channels=built)
    assert [c.name for c in dispatcher.channels] == ["log"]


def test_a_disabled_channel_is_still_validated(tmp_path, monkeypatch):
    _without_extras(monkeypatch)
    bad = [{**DISABLED_VOICE[0], "fifo_typo": "x"}, {**DISABLED_VOICE[1], "url": "not a url"}]
    built = build_channels(parse_config({"channels": bad}, tmp_path))
    assert built.disabled == [] and built.channels == []
    assert "unknown key" in built.errors["house"]
    assert "url" in built.errors["face"]


def test_check_config_passes_with_disabled_channels_and_no_extras(tmp_path, monkeypatch):
    _without_extras(monkeypatch)
    path = tmp_path / "scout.yaml"
    path.write_text(json.dumps({"channels": DISABLED_VOICE}))
    result = runner.invoke(app, ["check", "config", str(path)])
    assert result.exit_code == 0, result.output
    assert "channel house: disabled" in result.output and "install argus-scout" not in result.output


def test_deliver_to_a_disabled_channel_says_so(tmp_path, monkeypatch):
    _without_extras(monkeypatch)
    path = tmp_path / "scout.yaml"
    path.write_text(json.dumps({"channels": DISABLED_VOICE}))
    result = runner.invoke(app, ["deliver", "-c", str(path), "--latest", "--channel", "house"])
    assert result.exit_code == 1 and "channel house is disabled" in result.output


def test_text_clean_drops_format_characters_without_splitting_words():
    family = "\U0001f468\u200d\U0001f469"
    assert clean("co\u00adoperate \u202eabc\u202c x\u200by") == "cooperate abc xy"
    assert clean(f"{family} a\rb\tc\x00d\r\ne") == f"{family} a b    cd\ne"
