from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from argus_scout.approvals import StandingApprovals, StandingRule
from argus_scout.cycles import AnalysisCycle
from argus_scout.engine import RecommendationEngine
from argus_scout.prometheus import PrometheusUnavailable, Sample
from argus_scout.recommendations import Confidence, Recommendation, Status
from argus_scout.severity import Severity
from argus_scout.signals import Evaluation, SignalHit, SignalRulesFile
from argus_scout.store import SqliteStore

T0 = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
KEY = 'disk-filling{instance="node-a:9100",mountpoint="/data"}'


class Clock:
    def __init__(self, at: datetime = T0) -> None:
        self.at = at

    def __call__(self) -> datetime:
        return self.at

    def advance(self, **kwargs) -> None:
        self.at += timedelta(**kwargs)


def hit(
    key: str = KEY, *, rule: str = "disk-filling", severity: Severity = Severity.WARNING, value: float = 90.0, **extra
) -> SignalHit:
    fields = {
        "rule": rule,
        "key": key,
        "labels": {"instance": "node-a:9100", "mountpoint": "/data"},
        "value": value,
        "threshold": 85.0,
        "severity": severity,
        "since": T0,
        "title": f"/data on node-a:9100 is {value:.0f}% full",
        "action": "Free space on /data.",
        "impact": "low",
        "confidence": Confidence.HIGH,
    }
    fields.update(extra)
    return SignalHit(**fields)


@pytest.fixture
def store():
    with SqliteStore(":memory:") as s:
        yield s


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def engine(store, clock):
    return RecommendationEngine(store, StandingApprovals(), clock=clock)


def ran(*hits: SignalHit, rules=("disk-filling",), unknown=None) -> Evaluation:
    return Evaluation(hits, evaluated=rules, unknown=unknown)


# --- new and dedup --------------------------------------------------------------------------------------------


def test_new_hit_becomes_a_proposal_with_key_and_expiry(engine, store):
    report = engine.run_cycle(ran(hit()))
    [rec] = report.new
    assert rec.status == Status.PROPOSED
    assert rec.key == KEY and rec.analyzer == "disk-filling"
    assert rec.expires_at == T0 + timedelta(days=7)
    assert store.get_recommendation(rec.id).key == KEY
    assert [t.to_status for t in store.recommendation_history(rec.id)] == [Status.DRAFT, Status.PROPOSED]


def test_same_key_refreshes_never_duplicates(engine, store, clock):
    [rec] = engine.run_cycle(ran(hit())).new
    clock.advance(minutes=15)
    report = engine.run_cycle(ran(hit(value=97.0, severity=Severity.CRITICAL)))
    assert report.new == []
    [refreshed] = report.refreshed
    assert refreshed.id == rec.id
    assert refreshed.severity == Severity.CRITICAL
    assert refreshed.title == "/data on node-a:9100 is 97% full"
    assert refreshed.updated_at == clock.at and refreshed.last_seen_at == clock.at
    assert refreshed.expires_at == clock.at + timedelta(days=7)
    assert len(refreshed.evidence) == 2
    # Severity is raised, never lowered.
    clock.advance(minutes=15)
    [again] = engine.run_cycle(ran(hit(value=90.0))).refreshed
    assert again.severity == Severity.CRITICAL
    assert len(store.list_recommendations()) == 1


def test_one_cycle_with_a_repeated_key_is_one_recommendation(engine, store):
    report = engine.run_cycle(ran(hit(), hit(severity=Severity.CRITICAL, value=99)))
    [rec] = report.new
    assert rec.severity == Severity.CRITICAL
    assert len(store.list_recommendations()) == 1


def test_an_approved_recommendation_keeps_its_approved_wording(engine, store, clock):
    [rec] = engine.run_cycle(ran(hit())).new
    rec.approve()
    store.save_recommendation(rec, by="a person")
    clock.advance(minutes=15)
    [refreshed] = engine.run_cycle(ran(hit(value=93.0, title="changed"))).refreshed
    assert refreshed.status == Status.APPROVED
    assert refreshed.title == rec.title
    assert len(refreshed.evidence) == 2


# --- expiry ---------------------------------------------------------------------------------------------------


def test_cleared_signal_retires_after_grace(engine, store, clock):
    [rec] = engine.run_cycle(ran(hit())).new
    clock.advance(minutes=30)
    assert engine.run_cycle(ran()).retired == []  # within the grace period
    clock.advance(minutes=31)
    [retired] = engine.run_cycle(ran()).retired
    assert retired.id == rec.id and retired.status == Status.RETIRED
    assert "signal cleared" in retired.retired_reason
    last = store.recommendation_history(rec.id)[-1]
    assert (last.from_status, last.to_status, last.by) == (Status.PROPOSED, Status.RETIRED, "engine")
    assert "signal cleared" in last.reason


def test_unknown_never_clears(engine, store, clock):
    engine.run_cycle(ran(hit()))
    clock.advance(hours=6)
    report = engine.run_cycle(ran(rules=(), unknown={"disk-filling": "prometheus: down"}))
    assert report.retired == []
    assert report.unknown == {"disk-filling": "prometheus: down"}
    # A plain list with no hits says nothing about which rules ran: also not a clearance.
    assert engine.run_cycle([]).retired == []
    assert store.list_recommendations(Status.PROPOSED)


def test_past_expires_at_retires_even_while_unknown(engine, store, clock):
    [rec] = engine.run_cycle(ran(hit(expires_after=timedelta(hours=2)))).new
    assert rec.expires_at == T0 + timedelta(hours=2)
    clock.advance(hours=3)
    [retired] = engine.run_cycle(ran(rules=(), unknown={"disk-filling": "down"})).retired
    assert "expired" in retired.retired_reason


def test_approved_is_never_retired_by_the_engine(engine, store, clock):
    [rec] = engine.run_cycle(ran(hit())).new
    rec.approve()
    store.save_recommendation(rec)
    clock.advance(days=30)
    assert engine.run_cycle(ran()).retired == []
    assert store.get_recommendation(rec.id).status == Status.APPROVED


def test_old_rows_without_key_still_load_and_expire_only_by_date(store, engine, clock):
    legacy = Recommendation.from_dict({"id": "old", "title": "Defer backup", "status": "proposed", "analyzer": "power"})
    assert legacy.key == "" and legacy.expires_at is None
    store.save_recommendation(legacy)
    clock.advance(days=60)
    assert engine.run_cycle(ran(rules=("power",))).retired == []


# --- standing approvals and guardrails -------------------------------------------------------------------------


def test_standing_approval_applies_on_proposed(store, clock, write_file):
    path = write_file("approvals.yaml", "rules:\n  - id: disks\n    analyzer: disk-filling\n    max_impact: low\n")
    engine = RecommendationEngine(store, StandingApprovals(path), clock=clock)
    report = engine.run_cycle(ran(hit()))
    [rec] = report.auto_approved
    assert rec.status == Status.APPROVED  # approved, never further
    assert rec.approved_by_rule == "disks" and rec.approved_at == T0
    history = store.recommendation_history(rec.id)
    assert [t.to_status for t in history] == [Status.DRAFT, Status.PROPOSED, Status.APPROVED]
    assert history[-1].by == "rule:disks"


@pytest.mark.parametrize("guarded", [{"severity": Severity.CRITICAL}, {"impact": "high: takes the host offline"}])
def test_guardrails_refuse_auto_approval(store, clock, guarded):
    # A rule wider than the schema allows (max_impact: high), so its own scope refuses nothing: only the
    # guardrail stands between these hits and an auto-approval.
    approvals = StandingApprovals()
    approvals._api_rules["anything"] = StandingRule("anything", max_impact="high", min_confidence="low")
    engine = RecommendationEngine(store, approvals, clock=clock)
    report = engine.run_cycle(ran(hit(**guarded), hit("control")))
    [control] = report.auto_approved  # the same rule approves an unguarded hit
    assert control.key == "control" and control.approved_by_rule == "anything"
    [refused] = [r for r in report.new if r.key == KEY]
    assert refused.status == Status.PROPOSED


DISKS_RULE = "rules:\n  - id: disks\n    analyzer: disk-filling\n    max_impact: low\n"


def test_standing_approval_does_not_survive_escalation_past_a_guardrail(store, clock, write_file):
    engine = RecommendationEngine(store, StandingApprovals(write_file("approvals.yaml", DISKS_RULE)), clock=clock)
    [standing] = engine.run_cycle(ran(hit())).auto_approved
    clock.advance(minutes=5)
    report = engine.run_cycle(ran(hit(severity=Severity.CRITICAL, value=99)))

    [retired] = report.retired
    assert retired.id == standing.id
    old = store.get_recommendation(standing.id)
    assert old.status == Status.RETIRED and old.severity == Severity.WARNING
    assert old.retired_reason == "escalated past standing approval: critical signal"
    last = store.recommendation_history(standing.id)[-1]
    assert (last.from_status, last.to_status, last.by) == (Status.APPROVED, Status.RETIRED, "engine")

    [fresh] = report.new
    assert report.auto_approved == [] and report.refreshed == []
    assert fresh.id != standing.id and fresh.key == KEY
    assert fresh.status == Status.PROPOSED and fresh.severity == Severity.CRITICAL
    assert fresh.approval_source is None and standing.id in fresh.evidence[0]
    assert [r.id for r in store.recommendations_by_key(KEY, (Status.PROPOSED, Status.APPROVED))] == [fresh.id]

    clock.advance(minutes=5)  # the next cycle refreshes the new proposal: no duplicate, no second retirement
    again = engine.run_cycle(ran(hit(severity=Severity.CRITICAL, value=99)))
    assert [r.id for r in again.refreshed] == [fresh.id] and again.new == [] and again.retired == []


def test_escalated_standing_approval_is_reproposed_despite_an_old_rejection(store, clock, write_file):
    engine = RecommendationEngine(store, StandingApprovals(write_file("approvals.yaml", DISKS_RULE)), clock=clock)
    rejected = Recommendation(id="r1", key=KEY, title="old", severity=Severity.CRITICAL, status=Status.PROPOSED)
    store.save_recommendation(rejected)
    assert rejected.reject("not now", now=T0)
    store.save_recommendation(rejected)
    store.save_recommendation(
        Recommendation(
            id="s1",
            key=KEY,
            title="t",
            severity=Severity.WARNING,
            status=Status.APPROVED,
            approval_source="standing",
            approved_by_rule="disks",
            impact="low",
        )  # fmt: skip
    )
    report = engine.run_cycle(ran(hit(severity=Severity.CRITICAL, value=99)))
    assert [r.id for r in report.retired] == ["s1"] and report.suppressed == []
    assert report.new[0].status == Status.PROPOSED


def test_manual_approval_survives_escalation_with_a_note(engine, store, clock):
    [rec] = engine.run_cycle(ran(hit())).new
    assert rec.approve(now=T0)
    store.save_recommendation(rec)
    clock.advance(minutes=5)
    report = engine.run_cycle(ran(hit(severity=Severity.CRITICAL, value=99)))
    assert report.retired == [] and report.new == []
    got = store.get_recommendation(rec.id)
    assert got.status == Status.APPROVED and got.approval_source == "manual" and got.severity == Severity.CRITICAL
    assert "severity raised from warning to critical after approval" in got.evidence[-2]


def test_standing_approval_below_the_guardrail_keeps_its_approval(store, clock, write_file):
    engine = RecommendationEngine(store, StandingApprovals(write_file("approvals.yaml", DISKS_RULE)), clock=clock)
    [standing] = engine.run_cycle(ran(hit(severity=Severity.NOTICE))).auto_approved
    report = engine.run_cycle(ran(hit(severity=Severity.WARNING)))
    assert [r.id for r in report.refreshed] == [standing.id] and report.retired == []
    assert store.get_recommendation(standing.id).status == Status.APPROVED


# --- rejection memory -----------------------------------------------------------------------------------------


def _reject(store, rec: Recommendation, reason: str, at: datetime) -> None:
    rec.reject(reason, now=at)
    store.save_recommendation(rec, by="a person")


def test_rejected_key_is_not_reraised(engine, store, clock):
    [rec] = engine.run_cycle(ran(hit())).new
    _reject(store, rec, "that volume is scratch space", clock.at)
    clock.advance(days=90)
    report = engine.run_cycle(ran(hit()))
    assert report.new == [] and [h.key for h in report.suppressed] == [KEY]
    assert len(store.list_recommendations()) == 1


def test_escalation_beyond_the_rejected_severity_reraises(engine, store, clock):
    [rec] = engine.run_cycle(ran(hit())).new
    _reject(store, rec, "scratch space", clock.at)
    clock.advance(hours=1)
    assert engine.run_cycle(ran(hit(severity=Severity.NOTICE))).suppressed  # lower: still suppressed
    [new] = engine.run_cycle(ran(hit(severity=Severity.CRITICAL, value=99))).new
    assert new.id != rec.id
    assert new.evidence[0] == "previously rejected at warning on 2026-03-01: scratch space"


def test_rejection_ttl_lets_a_key_back(engine, store, clock):
    [rec] = engine.run_cycle(ran(hit(rejection_ttl=timedelta(days=7)))).new
    _reject(store, rec, "not now", clock.at)
    clock.advance(days=6)
    assert engine.run_cycle(ran(hit(rejection_ttl=timedelta(days=7)))).suppressed
    clock.advance(days=1)
    assert engine.run_cycle(ran(hit(rejection_ttl=timedelta(days=7)))).new


def test_recent_rejections_are_queryable(engine, store, clock):
    other = 'disk-filling{instance="node-b:9100",mountpoint="/"}'
    [a, b] = engine.run_cycle(ran(hit(), hit(other))).new
    _reject(store, a, "scratch", clock.at)
    _reject(store, b, "known, replacing the disk", clock.at + timedelta(minutes=1))
    rejections = engine.recent_rejections(5)
    assert [(r.key, r.reason) for r in rejections] == [(other, "known, replacing the disk"), (KEY, "scratch")]
    assert engine.recent_rejections(1)[0].key == other


# --- advisory only --------------------------------------------------------------------------------------------


def test_engine_never_moves_anything_past_approved(store, clock, write_file):
    path = write_file("approvals.yaml", "rules:\n  - id: all\n    analyzer: '*'\n    max_impact: medium\n")
    engine = RecommendationEngine(store, StandingApprovals(path), clock=clock)
    for i in range(5):
        engine.run_cycle(ran(hit(f"k{i}"), hit(f"c{i}", severity=Severity.CRITICAL)))
        clock.advance(days=3)
    recs = store.list_recommendations()
    assert recs and any(r.status == Status.APPROVED for r in recs)  # not vacuous: the rule did approve
    assert {r.status for r in recs} <= {Status.PROPOSED, Status.APPROVED, Status.RETIRED}
    assert all(r.status != Status.APPROVED for r in recs if r.severity == Severity.CRITICAL)


# --- the scheduled analysis cycle -----------------------------------------------------------------------------


class FakeProm:
    def __init__(self, answer) -> None:
        self.answer = answer

    async def query(self, promql: str, *, at: datetime | None = None) -> list[Sample]:
        if isinstance(self.answer, Exception):
            raise self.answer
        return [Sample(labels, value, at or T0) for labels, value in self.answer]


SIGNALS = """
rules:
  - name: disk-filling
    query: disk_used_percent
    threshold: 85
    labels_key: [instance, mountpoint]
    recommendation: {title: "{mountpoint} on {instance}", action: free space}
"""


async def test_analysis_cycle_raises_and_survives_an_outage(store, clock, write_file):
    prom = FakeProm([({"instance": "node-a:9100", "mountpoint": "/data"}, 90.0)])
    cycle = AnalysisCycle(store, rules=SignalRulesFile(write_file("signals.yaml", SIGNALS)), client=prom, clock=clock)
    report = await cycle()
    assert len(report.new) == 1
    prom.answer = PrometheusUnavailable("prometheus: could not connect")
    clock.advance(days=1)
    report = await cycle()  # doesn't raise, doesn't clear
    assert report.retired == [] and "disk-filling" in report.unknown
    assert len(store.list_recommendations(Status.PROPOSED)) == 1


async def test_analysis_cycle_degrades_without_config_or_with_a_broken_file(store, clock, write_file):
    assert await AnalysisCycle(store, clock=clock)() is None
    broken = SignalRulesFile(write_file("signals.yaml", "rules: ["))
    assert await AnalysisCycle(store, rules=broken, client=FakeProm([]), clock=clock)() is None


# --- escalation wording ---------------------------------------------------------------------------------------


def test_held_severity_and_wording_agree_after_escalation(engine, store, clock):
    """A cpu-hot proposal escalated to critical at 91°C and refreshed at 78°C keeps critical, and says so: the
    current value, the peak, and the escalation step's threshold, never "[critical] ... 78°C ... above 75°C"."""
    from argus_scout.signals import SignalRule, make_hit

    rule = SignalRule.model_validate(
        {
            "name": "cpu-hot",
            "query": "cpu_temp_celsius",
            "op": ">=",
            "threshold": 75,
            "escalate": [{"threshold": 85, "severity": "critical"}],
            "recommendation": {
                "title": "CPU on {instance} at {value:.0f}°C",
                "action": "Check the cooling on {instance}.",
                "rationale": "At or above {threshold:.0f}°C.",
            },
        }
    )

    def reading(value: float):
        return make_hit(rule, Sample({"instance": "host-a"}, value, T0), T0)

    [rec] = engine.run_cycle(ran(reading(80.0), rules=("cpu-hot",))).new
    assert rec.severity == Severity.WARNING and rec.peak == 80.0 and rec.rationale == "At or above 75°C."
    clock.advance(minutes=5)
    [rec] = engine.run_cycle(ran(reading(91.0), rules=("cpu-hot",))).refreshed
    assert rec.severity == Severity.CRITICAL and rec.peak == 91.0
    assert rec.title == "CPU on host-a at 91°C" and rec.rationale == "At or above 85°C."
    clock.advance(minutes=5)
    [rec] = engine.run_cycle(ran(reading(78.0), rules=("cpu-hot",))).refreshed
    assert rec.severity == Severity.CRITICAL and rec.peak == 91.0  # never lowered while open
    assert rec.title == "CPU on host-a at 78°C (peaked at 91°C)"
    assert rec.rationale == "At or above 85°C."
    assert rec.evidence[-1].endswith("; severity held at critical (peak 91)")
    stored = store.get_recommendation(rec.id)
    assert stored.peak == 91.0 and stored.title == rec.title
    clock.advance(minutes=5)
    [rec] = engine.run_cycle(ran(reading(88.0), rules=("cpu-hot",))).refreshed
    assert rec.title == "CPU on host-a at 88°C" and rec.peak == 91.0  # critical on its own again: no suffix


def test_a_held_severity_without_a_known_peak_keeps_the_plain_wording(engine, store, clock):
    """A recommendation raised before scout kept a peak doesn't get one invented."""
    [rec] = engine.run_cycle(ran(hit(value=97.0, severity=Severity.CRITICAL))).new
    rec.peak = None
    store.save_recommendation(rec, by="test")
    clock.advance(minutes=5)
    [rec] = engine.run_cycle(ran(hit(value=90.0))).refreshed
    assert rec.severity == Severity.CRITICAL and rec.peak is None
    assert rec.title == "/data on node-a:9100 is 90% full" and rec.evidence[-1].endswith("severity held at critical")


def test_a_null_peak_is_initialised_only_from_a_reading_at_least_as_severe(engine, store, clock):
    """A pre-0.1.3 row (peak null) takes the current reading as its peak when that reading is itself at the held
    severity, and keeps the plain wording until then: a milder, held reading can't stand in for a past peak."""
    [rec] = engine.run_cycle(ran(hit(value=97.0, severity=Severity.CRITICAL))).new
    rec.peak = None
    store.save_recommendation(rec, by="test")
    clock.advance(minutes=5)
    [rec] = engine.run_cycle(ran(hit(value=90.0))).refreshed  # milder: held, the peak stays unknown
    assert rec.peak is None
    clock.advance(minutes=5)
    [rec] = engine.run_cycle(ran(hit(value=96.0, severity=Severity.CRITICAL))).refreshed  # as severe: known now
    assert rec.severity == Severity.CRITICAL and rec.peak == 96.0
    assert store.get_recommendation(rec.id).peak == 96.0


def test_a_held_minimum_says_fell_to(engine, store, clock):
    """For a less-than rule the peak is the lowest reading: the title says "fell to", the evidence "lowest"."""
    from argus_scout.signals import SignalRule, make_hit

    rule = SignalRule.model_validate(
        {
            "name": "ups-battery",
            "query": "ups_battery_percent",
            "op": "<",
            "threshold": 50,
            "escalate": [{"threshold": 20, "severity": "critical"}],
            "recommendation": {"title": "UPS battery at {value:.0f}%", "action": "Check the UPS."},
        }
    )

    def reading(value: float):
        return make_hit(rule, Sample({"instance": "ups-a"}, value, T0), T0)

    engine.run_cycle(ran(reading(12.0), rules=("ups-battery",)))
    clock.advance(minutes=5)
    [rec] = engine.run_cycle(ran(reading(40.0), rules=("ups-battery",))).refreshed
    assert rec.severity == Severity.CRITICAL and rec.peak == 12.0
    assert rec.title == "UPS battery at 40% (fell to 12%)"
    assert rec.evidence[-1].endswith("; severity held at critical (lowest 12)")
