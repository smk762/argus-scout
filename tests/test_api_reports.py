"""Reports over HTTP: on-request reports through the briefing jobs' own path, the reads, and delivery."""

from __future__ import annotations

import asyncio
import threading

import pytest
from api_helpers import AUTH, NOW, TOKEN, scout_client
from test_channels import FakeSynth, Recorder, Speaker, bound
from test_narrative import BRIEFING, FakeGenerator, narrator, seed

from argus_scout.channels import Delivery, OpenGates
from argus_scout.config import parse_config
from argus_scout.cycles import BriefingJob, Briefings, ReportBusy
from argus_scout.server.app import create_app
from argus_scout.store import SqliteStore

SCHEDULED = "briefing:morning@07:30"


@pytest.fixture(autouse=True)
def token(monkeypatch):
    monkeypatch.setenv("SCOUT_API_TOKEN", TOKEN)


@pytest.fixture
def store(tmp_path):
    with SqliteStore(tmp_path / "scout.db") as s:
        yield s


def make_app(store, tmp_path, *, limits=None, block_manual=False, **server):
    config = parse_config(
        {
            "schedule": {"briefings": [{"kind": "morning", "at": "07:30"}]},
            "budget": {"limits": limits or {}, "block_manual": block_manual},
            "server": {"read_only": False, **server},
        },
        tmp_path,
    )
    return create_app(config, store=store, clock=lambda: NOW, run_scheduler=False)


class Rig:
    """A running app with a fake model, a text channel and a voice channel."""

    def __init__(self, client, generator=None):
        self.client = client
        self.runtime = client.app.state.runtime
        self.generator = generator or FakeGenerator(budget=self.runtime.budget)
        self.runtime.briefings.narrator.generator = self.generator
        self.log, self.speaker, self.synth = Recorder(), Speaker(), FakeSynth()
        dispatcher = self.runtime.dispatcher
        dispatcher.channels = [bound(self.log, "log"), bound(self.speaker, "speaker")]
        dispatcher.synthesiser, dispatcher.gates = self.synth, OpenGates()

    def post(self, path="/api/reports", **body):
        return self.client.post(path, headers=AUTH, json=body)


@pytest.fixture
def rig(store, tmp_path):
    with scout_client(make_app(store, tmp_path)) as client:
        yield Rig(client)


# --- generating ----------------------------------------------------------------------------------------------


def test_generate_stores_and_delivers(rig, store):
    seed(store)
    response = rig.post(kind="morning")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["kind"] == "morning" and body["text"] == BRIEFING
    assert body["meta"]["path"] == "llm" and body["meta"]["severity"] == "critical"
    assert body["meta"]["trigger"] == "request"
    assert store.get_report(body["id"]).text == BRIEFING
    assert body["delivered"] and sorted(body["delivery"]["sent"]) == ["log", "speaker"]
    assert len(rig.log.got) == 1 and len(rig.speaker.played) == 1 and rig.synth.calls


def test_generate_without_deliver_sends_and_speaks_nothing(rig, store):
    body = rig.post(kind="evening", deliver=False).json()
    assert body["delivered"] is False and body["delivery"] is None
    assert store.get_report(body["id"]) is not None
    assert rig.log.got == [] and rig.speaker.played == [] and rig.synth.calls == []
    assert rig.runtime.budget.used("speak") == 0 and rig.runtime.budget.used("tts") == 0
    assert store.delivered_channels(Delivery.from_report(store.get_report(body["id"])).key) == set()


def test_no_llm_uses_the_template(rig):
    body = rig.post(kind="status", no_llm=True, deliver=False).json()
    assert body["meta"]["path"] == "template" and body["meta"]["fallback_reason"] == "disabled"
    assert rig.generator.prompts == []  # the model was never asked


def test_bad_bodies_are_422(rig):
    assert rig.post(kind="weekly").status_code == 422
    assert rig.post().status_code == 422
    assert rig.post(kind="morning", dry_run=True).status_code == 422  # unknown keys are refused


@pytest.mark.parametrize("block_manual", [False, True])
def test_over_the_cap_falls_back_to_the_template(store, tmp_path, block_manual):
    """HTTP work normally counts as manual and may go over a cap; an on-request report never does."""
    app = make_app(store, tmp_path, limits={"llm_calls": 1, "speak": 1}, block_manual=block_manual)
    with scout_client(app) as client:
        rig = Rig(client)
        first = rig.post(kind="morning").json()
        assert first["meta"]["path"] == "llm"
        second = rig.post(kind="morning").json()
        assert second["meta"]["path"] == "template" and second["meta"]["fallback_reason"] == "budget"
        assert len(rig.generator.prompts) == 1 and rig.runtime.budget.used("llm_calls") == 1
        # Speech is capped the same way: the second report went to text only.
        assert second["delivery"]["sent"] == ["log"]
        assert second["delivery"]["results"][1]["status"] == "suppressed"
        assert len(rig.speaker.played) == 1


# --- one report of a kind at a time --------------------------------------------------------------------------


class Gated(FakeGenerator):
    """A model that holds its first call until released, without blocking the event loop."""

    def __init__(self):
        super().__init__("Held.")
        self.entered, self.release = threading.Event(), threading.Event()

    async def generate(self, prompt, **kw):
        if not self.entered.is_set():
            self.entered.set()
            await asyncio.to_thread(self.release.wait, 10)
        return await super().generate(prompt, **kw)


def test_a_request_while_the_same_kind_is_being_written_is_409(store, tmp_path):
    with scout_client(make_app(store, tmp_path)) as client:
        rig = Rig(client, Gated())
        first: dict = {}
        thread = threading.Thread(target=lambda: first.update(r=rig.post(kind="morning", deliver=False)))
        thread.start()
        try:
            assert rig.generator.entered.wait(5)
            busy = rig.post(kind="morning")
            assert busy.status_code == 409 and "being written" in busy.json()["detail"]
            # Another kind is not held up, and the loop is free: reads still answer.
            assert rig.post(kind="evening", deliver=False).status_code == 200
            assert client.get("/api/reports").status_code == 200
        finally:
            rig.generator.release.set()
            thread.join(10)
        assert first["r"].status_code == 200
        assert sorted(r.kind for r in store.list_reports()) == ["evening", "morning"]
        assert rig.log.got == []  # the refused request sent nothing
        assert rig.post(kind="morning", deliver=False).status_code == 200  # free again


def test_a_request_while_the_scheduled_job_runs_is_409(store, tmp_path):
    with scout_client(make_app(store, tmp_path)) as client:
        rig = Rig(client, Gated())
        job = rig.runtime.scheduler.jobs[SCHEDULED].func
        assert isinstance(job, BriefingJob) and job.briefings is rig.runtime.briefings  # one shared writer
        future = client.portal.start_task_soon(rig.runtime.scheduler.run_now, SCHEDULED)
        try:
            assert rig.generator.entered.wait(5)
            assert rig.post(kind="morning").status_code == 409
        finally:
            rig.generator.release.set()
        assert future.result(10) is True
        assert store.latest_report("morning").meta["trigger"] == "scheduled"
        assert len(rig.log.got) == 1  # the scheduled report was delivered, the refused request added nothing


async def test_the_scheduled_job_waits_for_an_on_request_report(store):
    gate = asyncio.Event()

    class Waits(FakeGenerator):
        async def generate(self, prompt, **kw):
            if not self.prompts:
                self.prompts.append(prompt)
                await gate.wait()
                return await super().generate(prompt, **kw)
            return await super().generate(prompt, **kw)

    delivered = []

    async def deliver(report):
        delivered.append(report.meta["trigger"])

    briefings = Briefings(narrator(store, Waits()), deliver=deliver)
    job = BriefingJob("morning", store, briefings=briefings)
    request = asyncio.create_task(briefings.write("morning", trigger="request", wait=False))
    await asyncio.sleep(0)
    scheduled = asyncio.create_task(job())
    await asyncio.sleep(0.01)
    assert briefings.busy("morning") and not scheduled.done()
    with pytest.raises(ReportBusy):
        await briefings.write("morning", wait=False)
    gate.set()
    await asyncio.gather(request, scheduled)
    assert delivered == ["request", "scheduled"]  # queued, not skipped, and never interleaved
    assert not briefings.busy("morning")


# --- reading -------------------------------------------------------------------------------------------------


def test_list_get_and_404(rig, store):
    ids = [rig.post(kind=k, deliver=False).json()["id"] for k in ("morning", "status", "morning")]
    listed = rig.client.get("/api/reports").json()
    assert listed["count"] == 3 and [r["id"] for r in listed["reports"]] == ids[::-1]  # newest first
    mornings = rig.client.get("/api/reports", params={"kind": "morning", "limit": 1}).json()
    assert [r["id"] for r in mornings["reports"]] == [ids[2]]
    assert rig.client.get("/api/reports", params={"limit": 0}).status_code == 422
    assert rig.client.get("/api/reports", params={"limit": 201}).status_code == 422
    assert rig.client.get("/api/reports", params={"kind": "weekly"}).status_code == 422

    one = rig.client.get(f"/api/reports/{ids[1]}").json()
    assert one["id"] == ids[1] and one["kind"] == "status" and "path" in one["meta"]
    assert rig.client.get("/api/reports/9999").status_code == 404
    assert rig.client.get("/api/reports/abc").status_code == 404


def test_latest_is_not_shadowed_by_an_id(rig):
    newest = rig.post(kind="evening", deliver=False).json()["id"]
    assert rig.client.get("/api/reports/latest").json()["id"] == newest
    assert rig.client.get("/api/reports/latest", params={"kind": "morning"}).status_code == 404


def test_read_auth_covers_the_report_reads(store, tmp_path):
    with scout_client(make_app(store, tmp_path, read_auth=True)) as client:
        report = store.add_report("morning", NOW, "hi", {})
        for path in ("/api/reports", f"/api/reports/{report.id}"):
            assert client.get(path).status_code == 401, path
            assert client.get(path, headers=AUTH).status_code == 200, path


# --- delivering a stored report ------------------------------------------------------------------------------


def test_deliver_is_idempotent_unless_forced(rig, store):
    report_id = rig.post(kind="morning", deliver=False).json()["id"]
    path = f"/api/reports/{report_id}/deliver"
    first = rig.post(path).json()
    assert first["report_id"] == report_id and sorted(first["sent"]) == ["log", "speaker"]
    again = rig.post(path).json()
    assert again["sent"] == [] and {r["status"] for r in again["results"]} == {"duplicate"}
    assert len(rig.log.got) == 1 and len(rig.speaker.played) == 1
    forced = rig.post(path, force=True).json()
    assert sorted(forced["sent"]) == ["log", "speaker"] and len(rig.log.got) == 2
    assert rig.runtime.budget.used("speak") == 2  # each spoken delivery is charged


def test_deliver_to_a_subset_of_channels(rig):
    report_id = rig.post(kind="morning", deliver=False).json()["id"]
    body = rig.post(f"/api/reports/{report_id}/deliver", channels=["log"]).json()
    assert body["sent"] == ["log"] and [r["channel"] for r in body["results"]] == ["log"]
    assert rig.speaker.played == [] and rig.synth.calls == []
    unknown = rig.post(f"/api/reports/{report_id}/deliver", channels=["log", "pager"])
    assert unknown.status_code == 422 and "pager" in unknown.json()["detail"]
    assert rig.post(f"/api/reports/{report_id}/deliver", channels=[]).status_code == 422


def test_deliver_unknown_report_or_no_channels(rig):
    assert rig.post("/api/reports/9999/deliver").status_code == 404
    report_id = rig.post(kind="morning", deliver=False).json()["id"]
    rig.runtime.dispatcher.channels = []
    assert rig.post(f"/api/reports/{report_id}/deliver").status_code == 409


def test_deliver_is_held_to_the_speech_cap(store, tmp_path):
    with scout_client(make_app(store, tmp_path, limits={"speak": 1})) as client:
        rig = Rig(client)
        report_id = rig.post(kind="morning", deliver=False).json()["id"]
        path = f"/api/reports/{report_id}/deliver"
        assert sorted(rig.post(path).json()["sent"]) == ["log", "speaker"]
        assert rig.post(path, force=True).json()["sent"] == ["log"]  # over the cap: text only
        assert len(rig.speaker.played) == 1


def test_a_refused_answer_is_readable_over_the_api_and_never_delivered(store, tmp_path):
    """``rejected_text`` is behind the read policy (``/api/reports``); channels never see it."""
    seed(store)
    hollow = "Good morning. " + "Everything is calm and there is nothing to add this morning. " * 4
    with scout_client(make_app(store, tmp_path)) as client:
        rig = Rig(client)
        rig.runtime.briefings.narrator.generator = FakeGenerator(hollow)
        body = rig.post(kind="morning").json()
        assert body["meta"]["fallback_reason"] == "hollow" and body["meta"]["rejected_check"]["missing"] == ["e1", "p1"]
        one = client.get(f"/api/reports/{body['id']}").json()
        assert one["meta"]["rejected_text"] == hollow.strip()
        [sent] = rig.log.got
        assert "Everything is calm" not in sent.text and "rejected_text" not in sent.meta
        assert sent.meta["rejected_check"]["rule"] == "missing_items"
        assert all("Everything is calm" not in said for said in rig.synth.calls)
