"""Status reports end to end: the severity, title and quiet exemption a report carries to the channels, the
per-item voice policy inside a status report, and when a group is news (pre-release review findings 1-3)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from argus_scout.channels import BoundChannel, Delivery, DeliveryResult, Dispatcher, HeartbeatGates, SpeechAudio
from argus_scout.config import TextChannel as TextChannelConfig
from argus_scout.cycles import StatusCycle
from argus_scout.events import StatusEvent
from argus_scout.heartbeat import PolicyFile
from argus_scout.inbox import Inbox, Outcome
from argus_scout.narrative import Narrator
from argus_scout.recommendations import Recommendation, Status
from argus_scout.severity import Severity
from argus_scout.store import Report, SqliteStore

T0 = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
NIGHT = datetime(2026, 6, 1, 23, 30, tzinfo=UTC)
QUIET = "quiet_hours: {start: '22:00', end: '07:00'}\n"


@pytest.fixture
def store(tmp_path):
    with SqliteStore(tmp_path / "scout.db") as s:
        yield s


class Clock:
    def __init__(self, at: datetime = T0) -> None:
        self.at = at

    def __call__(self) -> datetime:
        return self.at

    def advance(self, **kw) -> None:
        self.at += timedelta(**kw)


def ev(event_id: str, severity: str, summary: str, *, source: str = "mon", **kw) -> StatusEvent:
    return StatusEvent(event_id, source, Severity(severity), T0, summary, **kw)


class Cfg:
    def __init__(self, name: str, *, min_severity: str = "info") -> None:
        self.name, self.enabled, self.min_severity, self.kinds, self.timeout = name, True, min_severity, [], 5.0


class Recorder:
    kind = "fake"
    config_model = TextChannelConfig

    def __init__(self) -> None:
        self.got: list[Delivery] = []

    async def deliver(self, message: Delivery) -> DeliveryResult:
        self.got.append(message)
        return DeliveryResult(ok=True)


class Speaker(Recorder):
    def __init__(self) -> None:
        super().__init__()
        self.played: list[SpeechAudio] = []

    async def play(self, audio: SpeechAudio, message: Delivery) -> DeliveryResult:
        self.played.append(audio)
        return DeliveryResult(ok=True)


class FakeSynth:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def synthesise(self, text: str, message: Delivery) -> SpeechAudio:
        self.calls.append(text)
        return SpeechAudio(data=b"RIFF", text=text)


def status_cycle(store, policy: PolicyFile, clock, delivered: list[Report] | None = None, **kw) -> StatusCycle:
    async def deliver(report: Report) -> None:
        if delivered is not None:
            delivered.append(report)

    narrator = Narrator(store, clock=clock)
    return StatusCycle(store, policy, clock=clock, narrator=narrator, deliver=deliver, **kw)


# --- 1: a report carries the severity of its worst item -----------------------------------------------------


async def test_critical_status_report_reaches_a_critical_only_channel(store):
    clock = Clock()
    inbox = Inbox(store, clock=clock)
    inbox.ingest(ev("e1", "critical", "UPS on battery"))
    inbox.ingest(ev("e2", "warning", "Disk 90% full"))
    delivered: list[Report] = []
    result = await status_cycle(store, PolicyFile(None), clock, delivered)()
    assert result.report is not None
    meta = result.report.meta
    assert meta["severity"] == "critical" and meta["quiet_exempt"] is False
    assert meta["title"] == "Status update: UPS on battery (and 1 more)"

    critical_only, everything = Recorder(), Recorder()
    dispatcher = Dispatcher(
        [BoundChannel(critical_only, Cfg("pager", min_severity="critical")), BoundChannel(everything, Cfg("log"))]
    )
    out = await dispatcher.deliver(Delivery.from_report(delivered[0]))
    assert out.sent == ["pager", "log"]
    assert critical_only.got[0].severity is Severity.CRITICAL


async def test_warning_report_is_filtered_off_a_critical_only_channel(store):
    clock = Clock()
    Inbox(store, clock=clock).ingest(ev("e1", "warning", "Disk 90% full"))
    result = await status_cycle(store, PolicyFile(None), clock)()
    channel = Recorder()
    out = await Dispatcher([BoundChannel(channel, Cfg("pager", min_severity="critical"))]).deliver(
        Delivery.from_report(result.report)
    )
    assert out.get("pager").status == "filtered" and channel.got == []


async def test_briefing_severity_counts_items_and_open_recommendations(store):
    clock = Clock()
    narrator = Narrator(store, clock=clock)
    empty = await narrator.report("morning")
    assert empty.meta["severity"] == "info" and empty.meta["title"] == "Morning briefing"

    store.save_recommendation(Recommendation(id="p1", title="Shift backups", severity=Severity.WARNING,
                                             status=Status.PROPOSED))  # fmt: skip
    assert (await narrator.report("morning")).meta["severity"] == "warning"
    Inbox(store, clock=clock).ingest(ev("e1", "critical", "UPS on battery", quiet_exempt=True))
    briefing = await narrator.report("evening")
    assert briefing.meta["severity"] == "critical" and briefing.meta["quiet_exempt"] is True


async def test_quiet_exempt_status_report_is_spoken_in_quiet_hours(store, write_file):
    policy = PolicyFile(write_file("hb.yaml", QUIET))
    clock = Clock(NIGHT)
    Inbox(store, clock=clock).ingest(ev("e1", "warning", "Freezer door open", quiet_exempt=True))
    delivered: list[Report] = []
    result = await status_cycle(store, policy, clock, delivered)()
    assert result.quiet and result.decisions[0].voice
    assert result.report.meta["quiet_exempt"] is True

    speaker, synth = Speaker(), FakeSynth()
    dispatcher = Dispatcher([BoundChannel(speaker, Cfg("snap"))], synthesiser=synth, gates=HeartbeatGates(policy),
                            clock=clock)  # fmt: skip
    out = await dispatcher.deliver(Delivery.from_report(delivered[0]))
    assert out.sent == ["snap"], out.voice_rule
    assert synth.calls == ["Status update. Freezer door open."]


async def test_critical_status_report_is_spoken_in_quiet_hours(store, write_file):
    policy = PolicyFile(write_file("hb.yaml", QUIET))
    clock = Clock(NIGHT)
    Inbox(store, clock=clock).ingest(ev("e1", "critical", "UPS on battery"))
    delivered: list[Report] = []
    await status_cycle(store, policy, clock, delivered)()
    gates = HeartbeatGates(policy)
    allowed, rule = gates.voice(Delivery.from_report(delivered[0]), NIGHT)
    assert allowed, rule


# --- 2: the per-item voice policy inside a status report -------------------------------------------------------


async def test_text_only_and_unspeakable_items_are_not_spoken(store, write_file):
    policy = PolicyFile(write_file("hb.yaml", "sources:\n  backups:\n    channel: text\n"))
    clock = Clock()
    inbox = Inbox(store, clock=clock)
    inbox.ingest(ev("b1", "warning", "Backup of vault failed", source="backups"))
    inbox.ingest(ev("n1", "warning", "Router rebooted", speakable=False))
    delivered: list[Report] = []
    result = await status_cycle(store, policy, clock, delivered)()
    assert [d.voice for d in result.decisions] == [False, False]
    assert result.report.meta["speakable"] is False

    text, speaker, synth = Recorder(), Speaker(), FakeSynth()
    dispatcher = Dispatcher([BoundChannel(text, Cfg("log")), BoundChannel(speaker, Cfg("snap"))],
                            synthesiser=synth, gates=HeartbeatGates(policy))  # fmt: skip
    out = await dispatcher.deliver(Delivery.from_report(delivered[0]))
    assert out.sent == ["log"] and out.get("snap").status == "suppressed"
    assert synth.calls == [] and speaker.played == []


async def test_speech_holds_only_the_voice_eligible_items(store, write_file):
    policy = PolicyFile(write_file("hb.yaml", "sources:\n  backups:\n    channel: text\n"))
    clock = Clock()
    inbox = Inbox(store, clock=clock)
    inbox.ingest(ev("b1", "critical", "Backup of vault failed", source="backups"))
    inbox.ingest(ev("u1", "warning", "UPS on battery"))
    inbox.ingest(ev("i1", "notice", "Scrub started"))  # reported, but below voice.min_severity
    result = await status_cycle(store, policy, clock)()
    assert result.report.meta["speakable"] == "Status update. UPS on battery."
    assert "Backup" in result.report.text and "Scrub" in result.report.text  # text still has everything


async def test_speech_uses_an_events_own_spoken_text_when_it_has_one(store, write_file):
    policy = PolicyFile(write_file("hb.yaml", "{}\n"))
    clock = Clock()
    inbox = Inbox(store, clock=clock)
    inbox.ingest({"source": "mon", "event_id": "u1", "severity": "warning", "summary": "UPS: on_batt (load 41%)",
                  "speech": "The UPS is on battery!"})  # fmt: skip
    inbox.ingest(ev("d1", "warning", "Disk 90% full"))  # no speech: its summary is said
    inbox.ingest({"source": "mon", "event_id": "n1", "severity": "warning", "summary": "Router rebooted",
                  "speech": "The router rebooted", "speakable": False})  # fmt: skip
    result = await status_cycle(store, policy, clock)()
    assert result.report.meta["speakable"] == "Status update. The UPS is on battery! Disk 90% full."
    assert "UPS: on_batt" in result.report.text and "Router rebooted" in result.report.text  # text keeps summaries


async def test_unspeakable_report_skips_synthesis_whatever_the_gates_say(store):
    speaker, synth = Speaker(), FakeSynth()

    class Yes:
        def voice(self, message, now):
            return True, "always"

    dispatcher = Dispatcher([BoundChannel(speaker, Cfg("snap"))], synthesiser=synth, gates=Yes())
    message = Delivery.from_report(Report(id=1, kind="status", generated_at=T0, text="x", meta={"speakable": False}))
    out = await dispatcher.deliver(message)
    assert out.get("snap").status == "suppressed" and synth.calls == []


async def test_on_change_only_keeps_repeats_text_only(store, write_file):
    policy = PolicyFile(write_file("hb.yaml", "voice:\n  on_change_only: true\n"))
    clock = Clock()
    inbox = Inbox(store, clock=clock)
    inbox.ingest(ev("u1", "warning", "UPS on battery"))
    cycle = status_cycle(store, policy, clock)
    assert (await cycle()).report.meta["speakable"] == "Status update. UPS on battery."
    clock.advance(minutes=5)
    inbox.ingest(ev("d1", "warning", "Disk 90% full"))
    second = (await cycle()).report
    assert second.meta["speakable"] == "Status update. Disk 90% full."  # the repeat is in the text only
    assert "UPS on battery" in second.text

    policy = PolicyFile(write_file("hb2.yaml", "voice:\n  on_change_only: false\n"))
    clock.advance(minutes=5)
    inbox.ingest(ev("f1", "warning", "Fan failed"))
    third = (await status_cycle(store, policy, clock)()).report
    assert third.meta["speakable"].count(".") == 4  # title + all three


# --- 3: when a group is news -------------------------------------------------------------------------------------


async def test_a_condition_that_clears_and_recurs_is_reported_again(store):
    clock = Clock()
    inbox = Inbox(store, clock=clock)
    inbox.ingest(ev("e1", "warning", "Disk 90% full"))
    cycle = status_cycle(store, PolicyFile(None), clock, inbox=inbox)
    assert (await cycle()).report is not None
    clock.advance(minutes=5)
    inbox.resolve_missing("mon", [])
    assert (await cycle()).report is None
    clock.advance(days=2)
    assert inbox.ingest(ev("e1", "warning", "Disk 90% full")).outcome is Outcome.RECURRED
    again = (await cycle()).report
    assert again is not None and again.meta["facts"]["events"] == ["e1"]


async def test_a_new_item_is_news_and_the_old_one_rides_along(store):
    clock = Clock()
    inbox = Inbox(store, clock=clock)
    inbox.ingest(ev("a", "warning", "Disk 90% full"))
    cycle = status_cycle(store, PolicyFile(None), clock, inbox=inbox)
    assert (await cycle()).report is not None
    clock.advance(minutes=5)
    assert (await cycle()).report is None
    inbox.ingest(ev("b", "warning", "Fan failed"))
    report = (await cycle()).report
    assert report is not None and sorted(report.meta["facts"]["events"]) == ["a", "b"]


async def test_an_item_clearing_is_not_news(store):
    clock = Clock()
    inbox = Inbox(store, clock=clock)
    inbox.ingest(ev("a", "warning", "Disk 90% full"))
    inbox.ingest(ev("b", "warning", "Fan failed"))
    cycle = status_cycle(store, PolicyFile(None), clock, inbox=inbox)
    assert (await cycle()).report is not None
    clock.advance(minutes=5)
    inbox.resolve_missing("mon", ["a"])  # A, B down to A
    assert (await cycle()).report is None


async def test_an_item_clearing_reannounces_only_when_due(store, write_file):
    policy = PolicyFile(write_file("hb.yaml", "reannounce:\n  warning: 3600\n"))
    clock = Clock()
    inbox = Inbox(store, clock=clock)
    inbox.ingest(ev("a", "warning", "Disk 90% full"))
    cycle = status_cycle(store, policy, clock, inbox=inbox)
    assert (await cycle()).report is not None
    clock.advance(minutes=10)
    inbox.ingest(ev("b", "warning", "Fan failed"))
    assert (await cycle()).report is not None  # [A, B]
    clock.advance(minutes=10)
    inbox.resolve_missing("mon", ["a"])
    assert (await cycle()).report is None  # [A]: A was last reported (with B) 10 minutes ago
    clock.advance(minutes=40)
    assert (await cycle()).report is None  # 50 minutes since A was last reported
    clock.advance(minutes=10)
    assert (await cycle()).report is not None  # an hour: A's interval has passed


async def test_an_escalation_is_news(store):
    clock = Clock()
    inbox = Inbox(store, clock=clock)
    inbox.ingest(ev("e1", "warning", "Disk 90% full"))
    cycle = status_cycle(store, PolicyFile(None), clock, inbox=inbox)
    assert (await cycle()).report is not None
    clock.advance(minutes=5)
    assert inbox.ingest(ev("e1", "critical", "Disk 90% full")).outcome is Outcome.ESCALATED
    report = (await cycle()).report
    assert report is not None and report.meta["severity"] == "critical"


async def test_a_fresh_id_repeating_an_announced_item_is_not_news(store):
    clock = Clock()
    inbox = Inbox(store, clock=clock)
    inbox.ingest(ev("e1", "warning", "Disk 90% full"))
    cycle = status_cycle(store, PolicyFile(None), clock, inbox=inbox)
    assert (await cycle()).report is not None
    clock.advance(minutes=5)
    inbox.ingest(ev("e2", "warning", "Disk 90% full"))  # same fingerprint, new id
    assert (await cycle()).report is None


async def test_an_item_held_back_by_quiet_hours_is_reported_after_them(store, write_file):
    policy = PolicyFile(write_file("hb.yaml", QUIET))
    clock = Clock(NIGHT)
    inbox = Inbox(store, clock=clock)
    inbox.ingest(ev("w", "warning", "Disk 90% full"))
    inbox.ingest(ev("c", "critical", "UPS on battery"))
    cycle = status_cycle(store, policy, clock, inbox=inbox)
    night = (await cycle()).report
    assert night is not None and night.meta["facts"]["events"] == ["c"]
    clock.at = datetime(2026, 6, 2, 8, 0, tzinfo=UTC)
    morning = (await cycle()).report
    assert morning is not None and "w" in morning.meta["facts"]["events"]
