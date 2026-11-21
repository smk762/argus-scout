from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from argus_scout.events import StatusEvent, group_duplicates
from argus_scout.heartbeat import HeartbeatPolicy, PolicyFile, Window, apply_overrides, explain, reannounce_due

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "heartbeat.yaml"
NOW = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)


def event(event_id="e1", *, source="ci", severity="warning", **kw) -> StatusEvent:
    return StatusEvent.from_dict(
        {"event_id": event_id, "source": source, "severity": severity, "summary": f"{source} {event_id}", **kw}
    )


def test_defaults_without_file(tmp_path):
    policy = PolicyFile(tmp_path / "missing.yaml").get()
    assert not policy.file_loaded
    assert policy.voice_min_severity == "warning"
    assert set(policy.origin.values()) == {"default"}


def test_shipped_example_overrides_nothing():
    policy = PolicyFile(EXAMPLE).get()
    assert policy.file_loaded
    assert set(policy.origin.values()) == {"default"}
    assert policy.sources == {}
    assert policy.reannounce_after == {}


def test_file_overrides_defaults(write_file):
    path = write_file(
        "hb.yaml",
        "voice:\n  min_severity: critical\nquiet_hours:\n  start: '23:00'\n  end: '07:00'\n"
        "sources:\n  energy:\n    channel: text\n",
    )
    policy = PolicyFile(path).get()
    assert policy.voice_min_severity == "critical"
    assert policy.origin["voice.min_severity"] == "file"
    assert policy.quiet_hours.enabled
    assert policy.source_rule("energy")[0].channel == "text"
    assert policy.source_rule("other")[1] == "sources.default"


def test_file_reloads_on_change(write_file):
    path = write_file("hb.yaml", "voice:\n  min_severity: critical\n")
    pf = PolicyFile(path)
    assert pf.get().voice_min_severity == "critical"
    write_file("hb.yaml", "voice:\n  min_severity: notice\n")
    assert pf.get().voice_min_severity == "notice"


def test_defaults_are_overridable_by_caller(write_file):
    path = write_file("hb.yaml", "report:\n  max_items: 2\n")
    policy = PolicyFile(path, defaults=HeartbeatPolicy(voice_min_severity="critical")).get()
    assert policy.voice_min_severity == "critical"
    assert policy.report_max_items == 2


@pytest.mark.parametrize("text", ["- just\n- a list\n", "quiet_hours:\n  start: 'not a time'\n  end: '07:00'\n"])
def test_bad_file_falls_back_to_defaults(write_file, text):
    policy = PolicyFile(write_file("hb.yaml", text)).get()
    assert not policy.file_loaded
    assert policy.file_error


def test_unknown_channel_becomes_voice():
    policy = apply_overrides(HeartbeatPolicy(), {"sources": {"x": {"channel": "carrier-pigeon"}}})
    assert policy.sources["x"].channel == "voice"


def test_window_crosses_midnight():
    quiet = Window("23:00", "07:00")
    assert quiet.contains(datetime(2026, 1, 1, 23, 30))
    assert quiet.contains(datetime(2026, 1, 1, 6, 59))
    assert not quiet.contains(datetime(2026, 1, 1, 7, 0))
    assert not Window("", "07:00").contains(datetime(2026, 1, 1, 3, 0))


def test_briefing_source_never_interrupts():
    policy = apply_overrides(HeartbeatPolicy(), {"sources": {"ci": {"channel": "briefing"}}})
    decision = explain(event(severity="critical"), policy, now=NOW, quiet=False)
    assert decision.broadcast and not decision.report and not decision.voice
    assert decision.rule == "sources.ci.channel=briefing"


def test_text_source_reported_but_not_voiced():
    policy = apply_overrides(HeartbeatPolicy(), {"sources": {"energy": {"channel": "text"}}})
    decision = explain(event(source="energy", severity="critical"), policy, now=NOW, quiet=False)
    assert decision.report and not decision.voice


def test_unspeakable_event_stays_text_only():
    decision = explain(event(severity="critical", speakable=False), HeartbeatPolicy(), now=NOW, quiet=False)
    assert decision.report and not decision.voice
    assert "event.speakable=false" in decision.rule


def test_below_report_threshold_unless_ack_needed():
    policy = HeartbeatPolicy()
    assert not explain(event(severity="info"), policy, now=NOW, quiet=False).report
    decision = explain(event(severity="info", requires_ack=True), policy, now=NOW, quiet=False)
    assert decision.report and decision.rule.startswith("report.always_include:requires_ack")


def test_quiet_hours_let_only_critical_or_exempt_through():
    policy = HeartbeatPolicy()
    assert not explain(event(severity="warning"), policy, now=NOW, quiet=True).broadcast
    assert explain(event(severity="critical"), policy, now=NOW, quiet=True).broadcast
    assert explain(event(severity="notice", quiet_exempt=True), policy, now=NOW, quiet=True).broadcast


def test_acked_and_expired_never_broadcast():
    acked = event()
    acked.acked_at = NOW
    expired = event(expires_at=(NOW - timedelta(minutes=1)).isoformat())
    assert explain(acked, HeartbeatPolicy(), now=NOW, quiet=False).rule == "acked"
    assert explain(expired, HeartbeatPolicy(), now=NOW, quiet=False).rule == "expired"


def test_reannounce_only_with_rule_and_after_interval():
    policy = apply_overrides(HeartbeatPolicy(), {"reannounce": {"critical": 3600}})
    crit = event(severity="critical")
    assert not reannounce_due([crit], policy, NOW), "never broadcast yet"
    crit.last_broadcast_at = NOW - timedelta(minutes=30)
    assert not reannounce_due([crit], policy, NOW)
    crit.last_broadcast_at = NOW - timedelta(hours=2)
    assert reannounce_due([crit], policy, NOW)

    warn = event(severity="warning")
    warn.last_broadcast_at = NOW - timedelta(hours=5)
    assert not reannounce_due([warn], policy, NOW)


def test_duplicates_grouped_by_content_not_id():
    a = event("a", summary="Disk  FULL")
    b = event("b", summary="disk full")
    c = event("c", summary="disk full", severity="critical")
    assert [[e.event_id for e in g] for g in group_duplicates([a, b, c])] == [["a", "b"], ["c"]]


def test_event_needs_source_and_summary():
    with pytest.raises(ValueError, match="source"):
        StatusEvent.from_dict({"summary": "x"})
    with pytest.raises(ValueError, match="summary"):
        StatusEvent.from_dict({"source": "x"})
