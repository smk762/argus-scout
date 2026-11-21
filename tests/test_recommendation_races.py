"""A person's decision and an engine cycle racing on the same recommendation: the decision is never lost.

The engine loads a recommendation, works on it, and saves it later. If a person approves or rejects it through
the API in between, the engine's save must be refused (the store checks the status the engine loaded) and the
engine skips that item for the cycle. The other way round, a person who decides on a copy the engine has since
retired gets a 409, never a silent overwrite.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta

import pytest
from api_helpers import AUTH, TOKEN, make_app, scout_client
from test_engine import KEY, T0, Clock, hit, ran

from argus_scout.approvals import StandingApprovals
from argus_scout.engine import RecommendationEngine
from argus_scout.recommendations import Recommendation, Status
from argus_scout.store import UNCHECKED, IllegalTransition, SqliteStore, StaleRecommendation


class HookedStore(SqliteStore):
    """Runs ``before_save`` once, just before the next engine save: the window between load and save."""

    before_save: Callable[[], None] | None = None

    def save_recommendation(self, rec, *, expected_status=UNCHECKED, **kwargs) -> None:
        hook, self.before_save = self.before_save, None
        if hook is not None:
            hook()
        super().save_recommendation(rec, expected_status=expected_status, **kwargs)


@pytest.fixture(autouse=True)
def token(monkeypatch):
    monkeypatch.setenv("SCOUT_API_TOKEN", TOKEN)


@pytest.fixture
def store():
    with HookedStore(":memory:") as s:
        yield s


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def engine(store, clock):
    return RecommendationEngine(store, StandingApprovals(), clock=clock)


@pytest.fixture
def client(store, tmp_path):
    with scout_client(make_app(store, tmp_path)) as c:
        yield c


def proposed(engine) -> Recommendation:
    [rec] = engine.run_cycle(ran(hit())).new
    assert rec.status is Status.PROPOSED
    return rec


def decide(client, rec_id: str, verb: str = "approve"):
    """What a person does: view the recommendation, then decide on the revision they saw."""
    revision = client.get(f"/api/recommendations/{rec_id}").json()["revision"]
    body = {"by": "sam", "reason": "mine", "revision": revision}
    return client.post(f"/api/recommendations/{rec_id}/{verb}", headers=AUTH, json=body)


# --- the person decides inside the engine's load-then-save window ----------------------------------------------


@pytest.mark.parametrize("verb", ["approve", "reject"])
def test_manual_decision_survives_a_concurrent_engine_refresh(engine, store, client, clock, verb):
    rec = proposed(engine)
    clock.advance(minutes=15)
    store.before_save = lambda: decide(client, rec.id, verb).raise_for_status()

    report = engine.run_cycle(ran(hit(value=95.0)))  # loads the proposal, then the person decides, then it saves

    assert report.refreshed == []  # the engine skipped it this cycle, without raising
    stored = store.get_recommendation(rec.id)
    assert stored.status is (Status.APPROVED if verb == "approve" else Status.REJECTED)
    if verb == "approve":
        assert stored.approval_source == "manual"
    history = [(t.from_status, t.to_status, t.by) for t in store.recommendation_history(rec.id)]
    assert history[-1][1:] == (stored.status, "sam")
    assert all(t.to_status is not Status.PROPOSED for t in store.recommendation_history(rec.id)[2:])

    # Next cycle the engine sees the decision: an approved one is refreshed (wording kept), a rejected one stays.
    clock.advance(minutes=15)
    engine.run_cycle(ran(hit(value=96.0)))
    assert store.get_recommendation(rec.id).status is stored.status


def test_manual_approval_survives_a_concurrent_engine_retire(engine, store, client, clock):
    rec = proposed(engine)
    clock.advance(hours=2)  # past the clear grace: the next cycle without the hit retires it
    store.before_save = lambda: decide(client, rec.id).raise_for_status()

    report = engine.run_cycle(ran())  # lists it as proposed, then the person approves, then it would retire it

    assert report.retired == []
    stored = store.get_recommendation(rec.id)
    assert stored.status is Status.APPROVED and stored.approval_source == "manual"
    # Approved is never retired by the engine, so the approval holds on later cycles too.
    clock.advance(hours=2)
    engine.run_cycle(ran())
    assert store.get_recommendation(rec.id).status is Status.APPROVED


def test_standing_approval_loses_to_a_person_who_decided_first(store, client, clock, write_file):
    rules = write_file("approvals.yaml", "rules:\n  - id: disks\n    analyzer: disk-filling\n    max_impact: low\n")
    engine = RecommendationEngine(store, StandingApprovals(rules), clock=clock)

    def reject_the_new_proposal() -> None:
        [rec] = store.list_recommendations(Status.PROPOSED)
        decide(client, rec.id, "reject").raise_for_status()

    calls = {"n": 0}
    original = store.save_recommendation

    def save(rec, **kwargs):
        calls["n"] += 1
        if calls["n"] == 3:  # draft insert, proposed, then the standing approval: reject just before it lands
            reject_the_new_proposal()
        original(rec, **kwargs)

    store.save_recommendation = save  # type: ignore[method-assign]
    report = engine.run_cycle(ran(hit()))

    [rec] = report.new
    assert report.auto_approved == []
    assert rec.status is Status.REJECTED and store.get_recommendation(rec.id).status is Status.REJECTED


# --- the engine saves first; the person decides on a stale copy ------------------------------------------------


def test_refresh_first_then_manual_approval_still_lands(engine, store, client, clock):
    rec = proposed(engine)
    clock.advance(minutes=15)
    engine.run_cycle(ran(hit(value=95.0)))  # a content refresh is not a decision: the status is unchanged
    assert decide(client, rec.id).status_code == 200
    stored = store.get_recommendation(rec.id)
    assert stored.status is Status.APPROVED and stored.title.endswith("95% full")


def test_retire_first_then_stale_manual_approval_is_refused(engine, store, client, clock, monkeypatch):
    rec = proposed(engine)
    stale = store.get_recommendation(rec.id)  # what the person's request loaded
    clock.advance(hours=2)
    engine.run_cycle(ran())
    assert store.get_recommendation(rec.id).status is Status.RETIRED

    # Replay the request's read with the copy it had before the engine retired it.
    monkeypatch.setattr(store, "get_recommendation", lambda rec_id: stale)
    response = decide(client, rec.id)
    assert response.status_code == 409
    assert "reload" in response.json()["detail"]
    monkeypatch.undo()
    assert store.get_recommendation(rec.id).status is Status.RETIRED


# --- the store's checks ----------------------------------------------------------------------------------------


@pytest.fixture
def plain():
    with SqliteStore(":memory:") as s:
        yield s


def test_store_refuses_an_illegal_transition(plain):
    rec = Recommendation(title="t", key=KEY, created_at=T0, updated_at=T0)
    plain.save_recommendation(rec)
    rec.transition(Status.PROPOSED)
    plain.save_recommendation(rec)
    rec.approve()
    plain.save_recommendation(rec, by="sam")

    rec.status = Status.PROPOSED  # approved -> proposed: not a lifecycle step
    with pytest.raises(IllegalTransition):
        plain.save_recommendation(rec, by="engine")
    rec.status = Status.REJECTED
    with pytest.raises(IllegalTransition):
        plain.save_recommendation(rec)
    assert plain.get_recommendation(rec.id).status is Status.APPROVED
    assert [t.to_status for t in plain.recommendation_history(rec.id)] == [
        Status.DRAFT,
        Status.PROPOSED,
        Status.APPROVED,
    ]


def test_store_refuses_a_stale_write_and_writes_nothing(plain):
    rec = Recommendation(title="before", created_at=T0, updated_at=T0, status=Status.PROPOSED)
    plain.save_recommendation(rec, expected_status=None)
    with pytest.raises(StaleRecommendation):
        plain.save_recommendation(rec, expected_status=None)  # "no row yet", but there is one

    loaded = plain.get_recommendation(rec.id)
    other = plain.get_recommendation(rec.id)
    other.approve()
    plain.save_recommendation(other, by="sam", expected_status=Status.PROPOSED)

    loaded.title = "after"
    loaded.updated_at = T0 + timedelta(minutes=1)
    with pytest.raises(StaleRecommendation):
        plain.save_recommendation(loaded, expected_status=Status.PROPOSED)
    stored = plain.get_recommendation(rec.id)
    assert (stored.title, stored.status) == ("before", Status.APPROVED)

    with pytest.raises(StaleRecommendation):
        plain.save_recommendation(Recommendation(title="ghost"), expected_status=Status.PROPOSED)
    assert plain.get_recommendation("ghost") is None
