from __future__ import annotations

import textwrap
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from argus_scout.prometheus import PrometheusUnavailable, Sample
from argus_scout.severity import Severity
from argus_scout.signals import (
    Evaluation,
    SignalRule,
    SignalRulesFile,
    SignalState,
    dedup_key,
    evaluate,
    format_duration,
    load_signal_rules,
    parse_duration,
    parse_rules,
    render,
    template_fields,
)
from argus_scout.store import SqliteStore

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "signals.yaml"
T0 = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)


def raw_rule(**extra) -> dict:
    raw = {
        "name": "disk-filling",
        "query": "disk_used_percent",
        "op": ">",
        "threshold": 85,
        "for": "10m",
        "labels_key": ["instance", "mountpoint"],
        "escalate": [{"threshold": 95, "severity": "critical"}],
        "recommendation": {
            "title": "{mountpoint} on {instance} is {value:.0f}% full",
            "action": "Free space on {mountpoint}.",
            "rationale": "Above {threshold:.0f}% for {for}.",
        },
    }
    raw.update(extra)
    return raw


def rule(**extra) -> SignalRule:
    return SignalRule.model_validate(raw_rule(**extra))


class FakeProm:
    """Answers each query from ``values``: {query: [(labels, value), ...]} or an exception."""

    def __init__(self, values: dict) -> None:
        self.values = values
        self.queries: list[str] = []

    async def query(self, promql: str, *, at: datetime | None = None) -> list[Sample]:
        self.queries.append(promql)
        answer = self.values.get(promql, [])
        if isinstance(answer, Exception):
            raise answer
        return [Sample(labels, value, at or T0) for labels, value in answer]


DISK_A = {"instance": "node-a:9100", "mountpoint": "/data"}
DISK_B = {"instance": "node-b:9100", "mountpoint": "/"}


# --- durations ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (90, timedelta(seconds=90)),
        ("30s", timedelta(seconds=30)),
        ("1h30m", timedelta(minutes=90)),
        ("7d", timedelta(days=7)),
        ("0s", timedelta(0)),
    ],
)
def test_parse_duration(raw, expected):
    assert parse_duration(raw) == expected


@pytest.mark.parametrize("raw", ["", "soon", "5x", "-1", -5, True, "1m1h"])
def test_parse_duration_refuses(raw):
    with pytest.raises(ValueError):
        parse_duration(raw)


def test_format_duration():
    assert format_duration(timedelta(minutes=90)) == "1h30m"
    assert format_duration(timedelta(0)) == "0s"


# --- templates ------------------------------------------------------------------------------------------------

ALLOWED = {"value", "threshold", "instance"}


def test_render_fills_labels_and_numbers():
    text = render(
        "{instance} at {value:.1f} (>{threshold})", {"instance": "node-a", "value": 91.26, "threshold": 85}, ALLOWED
    )
    assert text == "node-a at 91.3 (>85)"
    assert render("{value:.0%}", {"value": 0.5}, ALLOWED) == "50%"


def test_render_missing_label_is_a_placeholder_and_values_are_inert():
    # A label value that looks like a template is inserted as text, never formatted again.
    assert render("{instance}", {"instance": "{value.__class__}"}, ALLOWED) == "{value.__class__}"
    assert render("{instance}", {}, ALLOWED) == "?"
    assert render("{instance}", {"instance": "x" * 1000}, ALLOWED) == "x" * 200


@pytest.mark.parametrize(
    ("template", "match"),
    [
        ("{value.__class__}", "no attribute or index"),
        ("{instance[0]}", "no attribute or index"),
        ("{instance.__init__.__globals__}", "no attribute or index"),
        ("{0}", "no attribute or index"),
        ("{}", "positional"),
        ("{instance!r}", "conversions"),
        ("{value:>100000000}", "only a precision"),
        ("{value:{threshold}}", "nested"),
        ("{instance:.2f}", "only a precision"),
        ("{value:,.2f}", "only a precision"),
        ("{secret}", "not a reserved name"),
        ("{unclosed", "bad template"),
    ],
)
def test_template_tricks_are_refused(template, match):
    with pytest.raises(ValueError, match=match):
        template_fields(template, ALLOWED)
    with pytest.raises(ValueError):
        render(template, {"instance": "a", "value": 1.0, "threshold": 2.0}, ALLOWED)


def test_a_rule_with_a_bad_template_is_invalid():
    with pytest.raises(ValueError, match="recommendation.title"):
        rule(recommendation={"title": "{device}", "action": "x"})  # device isn't in labels_key or labels
    ok = rule(labels=["device"], recommendation={"title": "{device}", "action": "x"})
    assert "device" in ok.placeholders


# --- rule validation ------------------------------------------------------------------------------------------


def test_example_file_loads_cleanly():
    ruleset = load_signal_rules(EXAMPLE)
    assert ruleset.file_error is None
    assert ruleset.errors == []
    names = {r.name for r in ruleset.rules}
    # The hard-coded analyzers the example rules replace.
    assert {"disk-filling", "node-exporter-down", "high-load", "backup-stale", "cpu-hot", "gpu-hot"} <= names
    assert {"dns-slow", "interface-errors", "ups-battery-low", "ups-load-high", "cpu-heating-fast"} <= names


def test_bad_rules_are_skipped_and_reported_the_rest_load():
    good = raw_rule()
    data = {
        "rules": [
            good,
            {**good, "name": "no-query", "query": ""},
            {**good, "name": "bad-op", "op": "=~"},
            {**good, "name": "bad-for", "for": "soon"},
            {**good, "name": "typo", "threshhold": 3},
            {**good, "name": "disk-filling"},  # duplicate
            {**good, "name": "bad-escalate", "escalate": [{"threshold": 50, "severity": "critical"}]},
            {**good, "name": "bad-label", "labels_key": ["not a label"]},
            "not a mapping",
        ]
    }
    ruleset = parse_rules(data)
    assert [r.name for r in ruleset.rules] == ["disk-filling"]
    assert len(ruleset.errors) == 8
    joined = "\n".join(ruleset.errors)
    for text in ("no-query", "bad-op", "bad-for", "unknown key", "duplicate", "not past", "not a Prometheus label"):
        assert text in joined


@pytest.mark.parametrize(("text", "match"), [("rules: 3", "must be a list"), ("- a\n- b", "mapping"), ("rules: [", "")])
def test_unusable_files_degrade_to_no_rules(write_file, text, match):
    ruleset = load_signal_rules(write_file("s.yaml", text))
    assert ruleset.rules == []
    assert ruleset.file_error and match in ruleset.file_error


def test_rules_file_reloads_on_change_and_survives_a_bad_edit(write_file):
    path = write_file("signals.yaml", EXAMPLE.read_text())
    rules_file = SignalRulesFile(path)
    first = rules_file.get()
    assert len(first.rules) > 5
    assert rules_file.get() is first  # cached until it changes
    write_file(
        "signals.yaml",
        "rules:\n  - name: only\n    query: up\n    threshold: 0\n    recommendation: {title: t, action: a}\n  - name: broken\n",
    )
    second = rules_file.get()
    assert [r.name for r in second.rules] == ["only"]
    assert second.errors
    write_file("signals.yaml", ": : :")
    assert rules_file.get().file_error
    assert SignalRulesFile(None).get().rules == []
    assert SignalRulesFile(path.parent / "missing.yaml").get().rules == []


def test_dedup_key_is_stable_and_quoted():
    assert dedup_key("r", {"instance": "a", "x": "y"}, ["instance"]) == 'r{instance="a"}'
    assert dedup_key("r", {"instance": 'a"b'}, ["instance", "mountpoint"]) == 'r{instance="a\\"b",mountpoint=""}'


# --- evaluation -----------------------------------------------------------------------------------------------


async def test_for_duration_holds_across_cycles():
    prom = FakeProm({"disk_used_percent": [(DISK_A, 90.0), (DISK_B, 50.0)]})
    state = SignalState()
    r = rule()
    first = await evaluate([r], prom, T0, state=state)
    assert list(first) == [] and first.pending == 1 and first.evaluated == {"disk-filling"}
    assert list(await evaluate([r], prom, T0 + timedelta(minutes=5), state=state)) == []
    [hit] = await evaluate([r], prom, T0 + timedelta(minutes=10), state=state)
    assert hit.key == 'disk-filling{instance="node-a:9100",mountpoint="/data"}'
    assert hit.since == T0
    assert hit.title == "/data on node-a:9100 is 90% full"
    assert hit.rationale == "Above 85% for 10m."
    assert hit.severity == Severity.WARNING


async def test_condition_breaking_resets_the_clock():
    state = SignalState()
    r = rule()
    await evaluate([r], FakeProm({"disk_used_percent": [(DISK_A, 90.0)]}), T0, state=state)
    await evaluate([r], FakeProm({"disk_used_percent": [(DISK_A, 80.0)]}), T0 + timedelta(minutes=5), state=state)
    later = await evaluate(
        [r], FakeProm({"disk_used_percent": [(DISK_A, 90.0)]}), T0 + timedelta(minutes=10), state=state
    )
    assert list(later) == [] and later.pending == 1


async def test_for_duration_survives_a_restart(tmp_path):
    db = tmp_path / "scout.db"
    prom = FakeProm({"disk_used_percent": [(DISK_A, 90.0)]})
    with SqliteStore(db) as store:
        assert list(await evaluate([rule()], prom, T0, state=SignalState(store))) == []
    with SqliteStore(db) as store:  # a new process
        [hit] = await evaluate([rule()], prom, T0 + timedelta(minutes=11), state=SignalState(store))
    assert hit.since == T0


async def test_outage_is_unknown_and_keeps_pending_state():
    state = SignalState()
    r = rule()
    await evaluate([r], FakeProm({"disk_used_percent": [(DISK_A, 90.0)]}), T0, state=state)
    down = await evaluate(
        [r], FakeProm({"disk_used_percent": PrometheusUnavailable("down")}), T0 + timedelta(minutes=5), state=state
    )
    assert list(down) == []
    assert down.unknown == {"disk-filling": "down"}
    assert down.evaluated == set()
    assert state.pending("disk-filling")  # not reset by the outage


async def test_escalation_nan_and_duplicate_series():
    prom = FakeProm(
        {
            "disk_used_percent": [
                (DISK_A, 91.0),
                ({**DISK_A, "device": "sdb"}, 97.0),  # same identity labels: one instance, the worse one
                (DISK_B, float("nan")),
            ]
        }
    )
    [hit] = await evaluate([rule(**{"for": 0})], prom, T0)
    assert hit.severity == Severity.CRITICAL
    assert hit.value == 97.0


async def test_removed_rules_lose_their_pending_state():
    state = SignalState()
    await evaluate([rule()], FakeProm({"disk_used_percent": [(DISK_A, 90.0)]}), T0, state=state)
    await evaluate([], FakeProm({}), T0, state=state)
    assert state.pending("disk-filling") == {}


def test_evaluations_merge():
    a = Evaluation(evaluated={"x"}, unknown={"y": "down"}, pending=1)
    b = Evaluation(evaluated={"z"}, pending=2)
    merged = a.merge(b)
    assert merged.evaluated == {"x", "z"} and merged.unknown == {"y": "down"} and merged.pending == 3


def test_example_rules_have_placeholder_values_only():
    text = textwrap.dedent(EXAMPLE.read_text())
    for forbidden in ("192.168.", "10.0.", "/home/", "@"):
        assert forbidden not in text


# --- escalation wording ---------------------------------------------------------------------------------------


CPU_RULE = {
    "name": "cpu-hot",
    "query": "cpu_temp_celsius",
    "op": ">=",
    "threshold": 75,
    "severity": "warning",
    "escalate": [{"threshold": 85, "severity": "critical"}],
    "labels_key": ["instance"],
    "recommendation": {
        "title": "CPU on {instance} at {value:.0f}°C",
        "action": "Check the cooling on {instance}.",
        "rationale": "At or above {threshold:.0f}°C ({severity}).",
    },
}


def cpu(**extra) -> SignalRule:
    return SignalRule.model_validate({**CPU_RULE, **extra})


def test_threshold_renders_the_step_that_set_the_severity():
    from argus_scout.signals import make_hit

    hit = make_hit(cpu(), Sample({"instance": "host-a"}, 91.0, T0), T0)
    assert hit.severity == Severity.CRITICAL and hit.threshold == 85
    assert hit.title == "CPU on host-a at 91°C"
    assert hit.rationale == "At or above 85°C (critical)."  # the escalation step, not the base 75
    base = make_hit(cpu(), Sample({"instance": "host-a"}, 78.0, T0), T0)
    assert base.rationale == "At or above 75°C (warning)." and base.threshold == 75


def test_a_held_severity_renders_the_current_value_and_the_peak():
    from argus_scout.signals import make_hit

    hit = make_hit(cpu(), Sample({"instance": "host-a"}, 78.0, T0), T0)
    texts = hit.rerender(Severity.CRITICAL, 91.0)
    assert texts.title == "CPU on host-a at 78°C (peaked at 91°C)"
    assert texts.rationale == "At or above 85°C (critical)."
    # A title that quotes {peak} itself gets no suffix.
    own = make_hit(cpu(recommendation={**CPU_RULE["recommendation"], "title": "{value:.0f}°C, peak {peak:.0f}°C"}),
                   Sample({"instance": "host-a"}, 78.0, T0), T0)  # fmt: skip
    assert own.rerender(Severity.CRITICAL, 91.0).title == "78°C, peak 91°C"


def _ups(op: str, threshold: float, escalate: float | None) -> SignalRule:
    steps = [] if escalate is None else [{"threshold": escalate, "severity": "critical"}]
    return SignalRule.model_validate(
        {
            "name": "ups-battery",
            "query": "ups_battery_percent",
            "op": op,
            "threshold": threshold,
            "escalate": steps,
            "recommendation": {"title": "UPS battery at {value:.0f}%", "action": "Check the UPS."},
        }
    )


@pytest.mark.parametrize(
    ("op", "threshold", "escalate", "now", "peak", "title"),
    [
        (">=", 75, 85, 78.0, 91.0, "UPS battery at 78% (peaked at 91%)"),
        (">", 75, 85, 78.0, 91.0, "UPS battery at 78% (peaked at 91%)"),
        ("<", 50, 20, 40.0, 12.0, "UPS battery at 40% (fell to 12%)"),
        ("<=", 50, 20, 40.0, 12.0, "UPS battery at 40% (fell to 12%)"),
        # == and != have no direction (and can't escalate): never shown as held past a peak, so no suffix,
        # even for a severity raised some other way.
        ("==", 0, None, 0.0, 1.0, "UPS battery at 0%"),
        ("!=", 100, None, 40.0, 12.0, "UPS battery at 40%"),
    ],
)
def test_held_peak_wording_follows_the_rule_direction(op, threshold, escalate, now, peak, title):
    from argus_scout.signals import make_hit

    hit = make_hit(_ups(op, threshold, escalate), Sample({"instance": "ups-a"}, now, T0), T0)
    assert hit.rerender(Severity.CRITICAL, peak).title == title


def test_peak_is_a_numeric_placeholder():
    assert template_fields("{peak:.1f}", {"peak"}) == ["peak"]
    assert render("{peak:.1f}", {"peak": 91.04}, {"peak"}) == "91.0"
