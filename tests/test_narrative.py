"""The narrative: facts, prompt budgets cut by priority, the template fallback, and the jobs that store reports."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from argus_scout.budget import DailyBudget
from argus_scout.cli import app
from argus_scout.cycles import BriefingJob, StatusCycle
from argus_scout.events import StatusEvent
from argus_scout.heartbeat import PolicyFile
from argus_scout.inbox import Inbox
from argus_scout.llm import LLM_CALLS, Completion, LLMUnavailable, Prompt
from argus_scout.memory import SCOUT_FILE, Memory
from argus_scout.narrative import Facts, Narrator, build_prompt, fact_lines, select_lines
from argus_scout.persona import DEFAULT_PERSONA, PersonaFile
from argus_scout.recommendations import Recommendation, Status
from argus_scout.severity import Severity
from argus_scout.store import SqliteStore
from argus_scout.tariff import TariffFile

T0 = datetime(2026, 6, 1, 7, 30, tzinfo=UTC)
EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
REASON = "the NAS must stay on during backups"
BRIEFING = (
    "Good morning. The UPS is on battery, the one thing that needs attention: check the mains feed before the "
    "batteries run down. Shifting the backup window to 01:00 is waiting on your decision. Nothing else is open."
)
"""A plausible model briefing for :func:`seed`'s facts: long enough, and it names the critical item."""


@pytest.fixture
def store(tmp_path):
    with SqliteStore(tmp_path / "scout.db") as s:
        yield s


class FakeGenerator:
    """Stands in for TextGenerator: records prompts, returns canned text or raises, and honours a budget."""

    def __init__(
        self,
        text: str = BRIEFING,
        *,
        error: Exception | None = None,
        budget=None,
        finish_reason: str | None = "stop",
        completion_tokens: int = 5,
    ) -> None:
        self.text, self.error, self.budget = text, error, budget
        self.finish_reason, self.completion_tokens = finish_reason, completion_tokens
        self.prompts: list[Prompt] = []
        self.model = "fake-model"

    async def generate(self, prompt: Prompt, *, max_tokens: int, kind: str = LLM_CALLS) -> Completion:
        from argus_scout.llm import LLMBudgetExceeded

        if self.budget is not None and not self.budget.try_consume(kind):
            raise LLMBudgetExceeded("over")
        self.prompts.append(prompt)
        if self.error:
            raise self.error
        return Completion(self.text, self.model, "fake", 10, self.completion_tokens, self.finish_reason)


def event(event_id: str, severity: str, summary: str, *, source: str = "mon") -> StatusEvent:
    return StatusEvent(event_id, source, Severity(severity), T0 - timedelta(minutes=5), summary)


def seed(store) -> None:
    inbox = Inbox(store, clock=lambda: T0)
    inbox.ingest(event("e1", "critical", "UPS on battery"))
    inbox.ingest(event("e2", "info", "Nightly scrub finished"))
    rec = Recommendation(id="p1", title="Shift the backup window", action="Start backups at 01:00",
                         severity=Severity.WARNING, status=Status.PROPOSED)  # fmt: skip
    store.save_recommendation(rec)
    rej = Recommendation(id="r1", title="Power down the NAS overnight", severity=Severity.NOTICE,
                         status=Status.PROPOSED)  # fmt: skip
    store.save_recommendation(rej)
    assert rej.reject(REASON, now=T0)
    store.save_recommendation(rej)


def narrator(store, generator=None, **kw) -> Narrator:
    return Narrator(store, generator=generator, clock=lambda: T0, **kw)


# --- prompt assembly and the budget --------------------------------------------------------------------------


async def test_prompt_holds_the_facts_and_quotes_rejections(store, tmp_path):
    seed(store)
    (tmp_path / "mem").mkdir()
    (tmp_path / "mem" / "prefs.md").write_text("Quote costs per month.")
    gen = FakeGenerator()
    n = narrator(store, gen, memory=Memory(tmp_path / "mem"), tariff=TariffFile(EXAMPLES / "tariff.yaml"))
    report = await n.report("morning")

    (prompt,) = gen.prompts
    assert prompt.system.startswith("Rules:") and "advisory only" in prompt.system
    user = prompt.user
    assert "[critical] mon: UPS on battery" in user and "[info] mon: Nightly scrub finished" in user
    assert "Shift the backup window: Start backups at 01:00" in user
    assert f'"Power down the NAS overnight" (notice), rejected 2026-06-01: reason "{REASON}"' in user
    assert "do not propose again or argue" in user
    assert "Energy tariff" in user and "prefs.md: Quote costs per month." in user
    assert user.index("UPS on battery") < user.index("Nightly scrub")  # most severe first

    assert report.id > 0 and report.text == BRIEFING and report.kind == "morning"
    meta = report.meta
    assert meta["path"] == "llm" and meta["model"] == "fake-model" and meta["fallback_reason"] is None
    assert set(meta["facts"]["events"]) == {"e1", "e2"} and meta["facts"]["rejections"] == ["r1"]
    assert meta["facts"]["proposed"] == ["p1"] and meta["facts"]["notes"] == ["prefs.md"]
    assert meta["facts"]["tariff"]["period"]
    # Scout's own rejections summary is kept in the memory directory.
    assert REASON in (tmp_path / "mem" / SCOUT_FILE).read_text()


async def test_rejections_are_quoted_from_the_store_when_the_memory_file_is_off(store, tmp_path):
    seed(store)
    (tmp_path / "mem").mkdir()
    (tmp_path / "mem" / "README.md").write_text("How this directory works, for people.")
    gen = FakeGenerator()
    await narrator(store, gen, memory=Memory(tmp_path / "mem", write_rejections=False)).report("morning")
    (prompt,) = gen.prompts
    assert f'"Power down the NAS overnight" (notice), rejected 2026-06-01: reason "{REASON}"' in prompt.user
    assert "How this directory works" not in prompt.user
    assert [p.name for p in (tmp_path / "mem").iterdir()] == ["README.md"]  # nothing written


def test_build_narrator_reads_memory_write_rejections(tmp_path):
    from argus_scout.config import parse_config
    from argus_scout.narrative import build_narrator

    config = parse_config({"files": {"memory": "mem"}, "memory": {"write_rejections": False}}, tmp_path)
    with SqliteStore(":memory:") as s:
        assert build_narrator(config, s, DailyBudget({})).memory.writes is False
        assert build_narrator(parse_config({"files": {"memory": "mem"}}, tmp_path), s, DailyBudget({})).memory.writes


def test_truncation_drops_least_severe_first():
    facts = Facts(kind="morning", at=T0)
    for i in range(40):
        facts.groups.append(_group(f"i{i}", "info", f"info item {i} " + "x" * 80))
    facts.groups.append(_group("c1", "critical", "the critical one"))
    facts.groups.append(_group("w1", "warning", "the warning one"))
    prompt, selection = build_prompt(facts, DEFAULT_PERSONA, context_tokens=1024, max_tokens=300)

    assert "the critical one" in prompt.user and "the warning one" in prompt.user
    assert selection.omitted["items"] > 0
    assert f"({selection.omitted['items']} more not shown)" in prompt.user
    assert len(prompt.system) + len(prompt.user) <= (1024 - 300) * 4  # fits the context with the output
    kept = {ln.ref for ln in selection.lines}
    assert {"c1", "w1"} <= kept


def test_selection_is_monotonic_by_tier():
    facts = Facts(kind="morning", at=T0, groups=[_group(f"e{i}", s, "x" * 100) for i, s in
                  enumerate(["info", "critical", "notice", "warning", "info"])])  # fmt: skip
    lines = fact_lines(facts)
    for budget in range(0, 800, 37):
        sel = select_lines(lines, budget)
        kept_tiers = [ln.tier for ln in sel.lines]
        dropped = [ln.tier for ln in lines if ln not in sel.lines]
        if kept_tiers and dropped:
            assert max(kept_tiers) <= min(dropped)


def test_status_prompt_covers_only_items_and_rejections():
    facts = Facts(kind="status", at=T0, groups=[_group("e1", "warning", "disk 91%")],
                  proposed=[Recommendation(id="p", title="Proposal", status=Status.PROPOSED)])  # fmt: skip
    prompt, _ = build_prompt(facts, DEFAULT_PERSONA)
    assert "disk 91%" in prompt.user and "Proposal" not in prompt.user


def _group(event_id: str, severity: str, summary: str):
    from argus_scout.inbox import EventGroup

    e = event(event_id, severity, summary)
    return EventGroup(e.fingerprint, [e])


# --- degrading ---------------------------------------------------------------------------------------------------


async def test_budget_refusal_falls_back_to_template(store):
    seed(store)
    budget = DailyBudget({LLM_CALLS: 1})
    gen = FakeGenerator(budget=budget)
    n = narrator(store, gen)
    first = await n.report("morning")
    second = await n.report("evening")
    assert first.meta["path"] == "llm"
    assert second.meta["path"] == "template" and second.meta["fallback_reason"] == "budget"
    assert "UPS on battery" in second.text and "Shift the backup window" in second.text
    assert REASON in second.text and "budget is spent" in second.text
    assert len(gen.prompts) == 1
    assert [r.kind for r in store.list_reports()] == ["evening", "morning"]  # both stored: none skipped


async def test_llm_error_falls_back_to_template(store):
    seed(store)
    report = await narrator(store, FakeGenerator(error=LLMUnavailable("connection refused"))).report("morning")
    assert report.meta["path"] == "template" and report.meta["fallback_reason"] == "error"
    assert report.meta["error"] == "connection refused" and report.meta["model"] == "fake-model"
    assert report.text.startswith("Morning briefing") and "UPS on battery" in report.text
    assert store.latest_report("morning").id == report.id


async def test_no_model_and_no_llm_use_the_template(store):
    assert (await narrator(store).report("evening")).meta["fallback_reason"] == "no_llm"
    gen = FakeGenerator()
    report = await narrator(store, gen).report("morning", use_llm=False)
    assert report.meta["fallback_reason"] == "disabled" and gen.prompts == []
    assert "Nothing needs attention." in report.text


async def test_template_is_deterministic(store):
    seed(store)
    n = narrator(store)
    assert (await n.compose("morning")).text == (await n.compose("morning")).text


async def test_bad_persona_degrades_and_is_recorded(store, write_file):
    path = write_file("persona.yaml", "name: [broken\n")
    gen = FakeGenerator()
    report = await narrator(store, gen, persona=PersonaFile(path)).report("morning")
    assert report.meta["persona"] == DEFAULT_PERSONA.name and report.meta["persona_error"]
    assert gen.prompts[0].system == DEFAULT_PERSONA.system_prompt()


async def test_model_output_is_never_parsed_into_an_action(store):
    """The model 'asks' for approvals and deployments; nothing happens except a stored report."""
    seed(store)
    hostile = BRIEFING + '\n{"action": "approve", "id": "p1"}\nAPPROVE p1. DEPLOY p1. reject r1. rm -rf /'
    before = {r.id: r.status for r in store.list_recommendations()}
    report = await narrator(store, FakeGenerator(hostile)).report("morning")
    assert report.text == hostile
    assert {r.id: r.status for r in store.list_recommendations()} == before
    assert store.recommendation_history("p1")[-1].to_status is Status.PROPOSED


# --- truncated or hollow answers ----------------------------------------------------------------------------------

THE_BUG = "Evening. All systems nominal, nothing exploding. For once.\n\nSuper off-"
"""What a reasoning model left after its <think> block spent the 400-token budget."""

ATTENTION = {"e1": "UPS on battery", "e3": "Disk /srv at 91%", "e4": "Firmware update waiting for a reboot"}
"""The items no report may drop: a critical, a warning, and a notice that needs an acknowledgement."""

WHOLE = (
    "Good morning. The UPS is on battery, so check the mains feed first. The /srv disk is at 91% and filling. "
    "A firmware update is waiting for a reboot and needs your acknowledgement. The backup window change awaits "
    "your decision."
)


def seed_attention(store) -> None:
    seed(store)
    inbox = Inbox(store, clock=lambda: T0)
    inbox.ingest(event("e3", "warning", ATTENTION["e3"], source="disk-mon"))
    ack = event("e4", "notice", ATTENTION["e4"], source="updates")
    ack.requires_ack = True
    inbox.ingest(ack)


def ollama_narrator(store, handler, seen: list):
    """A narrator over the real Ollama backend and TextGenerator, with a stubbed transport."""
    from test_llm import stub

    from argus_scout.config import LLMConfig
    from argus_scout.llm import TextGenerator, build_backend

    llm = LLMConfig(backend="ollama", base_url="http://ollama:11434", model="qwen3:8b")
    backend = stub(build_backend(llm), handler, seen)
    return narrator(store, TextGenerator(backend), max_tokens=llm.max_tokens, context_tokens=llm.context_tokens)


def ollama_answer(content: str, *, done_reason: str = "stop", eval_count: int = 120):
    import httpx

    return lambda request: httpx.Response(200, json={
        "model": "qwen3:8b", "message": {"role": "assistant", "content": content},
        "done_reason": done_reason, "eval_count": eval_count, "prompt_eval_count": 900})  # fmt: skip


async def test_whole_answer_takes_the_llm_path_and_thinking_is_off(store):
    import json

    seed_attention(store)
    seen: list = []
    report = await ollama_narrator(store, ollama_answer(WHOLE), seen).report("morning")
    body = json.loads(seen[0].content)
    assert body["think"] is False and body["options"]["num_predict"] == 800  # the morning default
    assert report.meta["path"] == "llm" and report.meta["fallback_reason"] is None and report.text == WHOLE
    assert report.meta["finish_reason"] == "stop" and report.meta["max_tokens"] == 800


async def test_token_limit_falls_back_as_truncated(store):
    seed_attention(store)
    answer = ollama_answer(THE_BUG, done_reason="length", eval_count=400)
    report = await ollama_narrator(store, answer, []).report("morning")
    meta = report.meta
    assert meta["path"] == "template" and meta["fallback_reason"] == "truncated"
    assert meta["completion_tokens"] == 400 and meta["finish_reason"] == "length" and meta["llm_chars"] == len(THE_BUG)
    assert "cut off" in report.text and "Super off-" not in report.text
    assert all(summary in report.text for summary in ATTENTION.values())


async def test_think_only_answer_falls_back(store):
    seed_attention(store)
    answer = ollama_answer("<think>The UPS is on battery, the disk is filling, so I should</think>")
    report = await ollama_narrator(store, answer, []).report("morning")
    assert report.meta["path"] == "template" and report.meta["fallback_reason"] == "hollow"
    assert report.meta["llm_chars"] == 0


async def test_answer_missing_a_critical_item_falls_back_as_hollow(store):
    seed_attention(store)
    hollow = WHOLE.replace("The UPS is on battery, so check the mains feed first. ", "Power is fine. ")
    assert len(hollow) >= 160
    report = await narrator(store, FakeGenerator(hollow)).report("morning")
    assert report.meta["path"] == "template" and report.meta["fallback_reason"] == "hollow"
    assert "e1" in report.meta["error"] and "UPS on battery" in report.text


async def test_short_status_answer_falls_back_and_a_whole_one_is_used(store):
    seed_attention(store)
    short = await narrator(store, FakeGenerator("UPS. Disk. Reboot.")).report("status")
    assert short.meta["fallback_reason"] == "hollow"
    whole = await narrator(store, FakeGenerator(WHOLE)).report("status")
    assert whole.meta["path"] == "llm"


async def test_nothing_to_report_accepts_one_short_sentence(store):
    report = await narrator(store, FakeGenerator("Nothing needs attention today.")).report("morning")
    assert report.meta["path"] == "llm"


@pytest.mark.parametrize("kind", ["morning", "evening", "status"])
@pytest.mark.parametrize(
    "answer",
    [
        FakeGenerator(THE_BUG, finish_reason="length", completion_tokens=400),
        FakeGenerator(THE_BUG),
        FakeGenerator(""),
        FakeGenerator(WHOLE.replace("A firmware update is waiting for a reboot and needs your acknowledgement. ", "")),
        FakeGenerator(WHOLE, finish_reason="length"),
    ],
    ids=["truncated", "short", "empty", "drops-the-ack-item", "whole-but-cut-off"],
)
async def test_a_failed_model_never_drops_a_warning_critical_or_ack_item(store, kind, answer):
    """The must-never: whatever the model got wrong, every item that needs attention is in the delivered text."""
    seed_attention(store)
    report = await narrator(store, answer).report(kind)
    assert report.meta["path"] == "template" and report.meta["fallback_reason"] in {"truncated", "hollow"}
    for summary in ATTENTION.values():
        assert summary in report.text, (kind, summary)


def test_template_lists_every_item_needing_attention_past_its_cap():
    from argus_scout.narrative import TEMPLATE_MAX_PER_SECTION, render_template

    groups = [_group(f"w{i}", "warning", f"Warning number {i}") for i in range(TEMPLATE_MAX_PER_SECTION + 2)]
    groups += [_group(f"i{i}", "info", f"Info number {i}") for i in range(3)]
    text = render_template(Facts(kind="status", at=T0, groups=groups), DEFAULT_PERSONA)
    assert all(f"Warning number {i}" in text for i in range(TEMPLATE_MAX_PER_SECTION + 2))
    assert "and 3 more" in text  # only the info items are counted, not listed


AUDITOR = "Rest completed: 27 confirmed, 0 draft PRs, 4 decision(s) (needs an acknowledgement)"
"""The real case: a briefing was refused as hollow for leaving this item out, though it named it."""


def _item(summary: str, source: str = "mon", **payload):
    from argus_scout.inbox import EventGroup

    e = StatusEvent("x1", source, Severity.WARNING, T0, summary, payload=payload)
    return EventGroup(e.fingerprint, [e])


MENTIONS = [
    # (answer, summary, source, payload, named?)
    # -- the real case, both ways
    (
        "The code auditor finished its rest run with twenty-seven confirmed and four decisions to acknowledge.",
        AUDITOR,
        "ai-code-auditor",
        {},
        True,
    ),
    ("Systems are fine; restarting nothing.", AUDITOR, "ai-code-auditor", {}, False),
    ("The auditor wants four decisions from you.", AUDITOR, "ai-code-auditor", {}, True),  # a source part
    ("Twenty-seven findings were confirmed overnight.", AUDITOR, "ai-code-auditor", {}, True),  # 27 + confirmed
    (
        "Nothing was confirmed and no decision is pending.",
        AUDITOR,
        "ai-code-auditor",
        {},
        True,
    ),  # 2 weak words (naming, not accuracy)
    ("Four things are waiting and none is a draft.", AUDITOR, "ai-code-auditor", {}, False),  # one weak word
    ("Zero problems, four green checks.", AUDITOR, "ai-code-auditor", {}, False),  # 0 and 4 are too short
    ("Get some rest tonight.", AUDITOR, "ai-code-auditor", {}, False),  # one weak word is not enough
    # -- whole words, with plural and stem tolerance
    ("The /srv disk is nearly full.", "Disk /srv at 91%", "mon", {}, True),
    ("Usage hit ninety-one percent on the disk.", "Disk /srv at 91%", "mon", {}, True),  # 91 spelled out + disk
    ("Usage hit 91% overnight.", "Disk /srv at 91%", "mon", {}, False),  # one weak word
    ("Both disks are nearly full.", "Disk /srv at 91%", "disk-mon", {}, True),  # "disks" ~ the source part "disk"
    ("The UPS fan stopped.", "UPS fan", "mon", {}, True),
    ("The backups ran.", "UPS fan", "mon", {}, False),  # "ups" is not a word of "backups"
    ("The firmware is ready; it is waiting on a reboot.", "Firmware update waiting for a reboot", "fw", {}, True),
    ("Two reboots are scheduled for the firmware.", "Firmware update waiting for a reboot", "fw", {}, True),
    ("Rebooting is not needed.", "Firmware update waiting for a reboot", "fw", {}, False),  # one weak word
    # -- host and label values are strong: one is enough
    ("The NAS is running hot.", "CPU temperature high", "node-exporter", {"host": "nas-example"}, True),
    ("A machine is running hot.", "CPU temperature high", "node-exporter", {"host": "nas-example"}, False),
    (
        "The postgres container keeps restarting.",
        "Container restarts",
        "docker",
        {"labels": {"name": "postgres"}},
        True,
    ),
    # -- generic words never name anything
    ("A warning is open. Check it.", "Disk /srv at 91%", "mon", {}, False),
    ("Everything is fine at home; the servers are healthy.", "Disk /srv at 91%", "home-server", {}, False),
]


@pytest.mark.parametrize(("answer", "summary", "source", "payload", "named"), MENTIONS)
def test_mentions_paraphrases_and_near_misses(answer, summary, source, payload, named):
    from argus_scout.narrative import mentions

    assert mentions(answer, _item(summary, source, **payload)) is named


@pytest.mark.parametrize(
    ("text", "digits"),
    [
        ("twenty-seven", "27"),
        ("four decisions", "4 decisions"),
        ("one hundred and five", "105"),
        ("nine hundred ninety-nine", "999"),
        ("zero", "0"),
        ("Ninety Nine", "99"),
        ("seven and eight", "seven and eight"),  # two numbers, not one: left alone
        ("someone tonight", "someone tonight"),  # number words inside other words are not numbers
    ],
)
def test_numbers_to_digits(text, digits):
    from argus_scout.narrative import numbers_to_digits

    assert numbers_to_digits(text) == digits.lower()


# --- the jobs ------------------------------------------------------------------------------------------------------


async def test_briefing_job_stores_and_delivers(store):
    seed(store)
    delivered = []

    async def deliver(report):
        delivered.append(report)

    job = BriefingJob("morning", store, narrator=narrator(store, FakeGenerator()), deliver=deliver,
                      clock=lambda: T0)  # fmt: skip
    report = await job()
    assert store.latest_report("morning").id == report.id and delivered == [report]
    assert job.runs == [T0]


async def test_briefing_job_without_a_model_still_stores(store):
    report = await BriefingJob("evening", store, narrator=narrator(store), clock=lambda: T0)()
    assert report is not None and report.meta["path"] == "template"
    assert store.latest_report("evening").text == report.text


async def test_status_cycle_reports_what_the_gate_allows_once(store, write_file):
    policy = write_file("heartbeat.yaml", "report:\n  min_severity: warning\nreannounce:\n  critical: 3600\n")
    seed(store)
    now = [T0]
    gen = FakeGenerator("UPS on battery.")
    cycle = StatusCycle(store, PolicyFile(policy), clock=lambda: now[0],
                        narrator=Narrator(store, generator=gen, clock=lambda: now[0]))  # fmt: skip
    first = await cycle()
    assert first.report is not None and first.report.kind == "status"
    assert first.report.meta["facts"]["events"] == ["e1"]  # the info item didn't pass the report gate
    assert "Nightly scrub" not in gen.prompts[0].user

    now[0] = T0 + timedelta(minutes=5)
    assert (await cycle()).report is None  # unchanged: not news

    now[0] = T0 + timedelta(hours=1, minutes=1)
    assert (await cycle()).report is not None  # critical re-announce interval passed
    assert len(store.list_reports(kind="status")) == 2


async def test_status_cycle_without_reportable_items_writes_nothing(store):
    Inbox(store, clock=lambda: T0).ingest(event("e2", "info", "Nightly scrub finished"))
    cycle = StatusCycle(store, PolicyFile(None), clock=lambda: T0, narrator=narrator(store, FakeGenerator()))
    assert (await cycle()).report is None and store.list_reports() == []


# --- the CLI ---------------------------------------------------------------------------------------------------------


def test_cli_brief_prints_and_stores(tmp_path):
    config = tmp_path / "scout.yaml"
    config.write_text("store: {path: scout.db}\nbudget: {limits: {llm_calls: 5}}\n")
    runner = CliRunner()
    result = runner.invoke(app, ["brief", "--config", str(config), "--kind", "evening", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "Evening review" in result.output and "template (no_llm)" in result.output and "dry run" in result.output
    assert not (tmp_path / "scout.db").exists()

    result = runner.invoke(app, ["brief", "--config", str(config), "--kind", "status", "--no-llm"])
    assert result.exit_code == 0, result.output
    assert "No open status items." in result.output and "template (disabled)" in result.output
    with SqliteStore(tmp_path / "scout.db") as s:
        assert s.latest_report("status") is not None

    assert runner.invoke(app, ["brief", "--config", str(config), "--kind", "weekly"]).exit_code == 1


# --- a refused answer is kept for audit, never delivered ------------------------------------------------------


async def test_a_hollow_answer_is_kept_for_audit_and_never_delivered(store):
    import json

    from argus_scout.channels import Delivery
    from argus_scout.channels.webhook import build_payload

    seed_attention(store)
    hollow = WHOLE.replace("The UPS is on battery, so check the mains feed first. ", "Power is fine.\x1b[2J ")
    report = await narrator(store, FakeGenerator(hollow)).report("morning")
    meta = report.meta
    assert meta["path"] == "template" and meta["fallback_reason"] == "hollow"
    # Stored, cleaned: the verdict can be audited from the report alone.
    assert meta["rejected_text"].startswith("Good morning.") and "Power is fine." in meta["rejected_text"]
    assert "\x1b" not in meta["rejected_text"]
    assert meta["rejected_check"] == {
        "reason": "hollow",
        "rule": "missing_items",
        "detail": "the model's answer left out e1",
        "missing": ["e1"],
    }
    assert store.get_report(report.id).meta["rejected_text"] == meta["rejected_text"]
    # Never delivered: channels read the report text, and the delivery meta drops the refused answer.
    assert "Power is fine" not in report.text
    message = Delivery.from_report(report)
    assert "rejected_text" not in message.meta and message.meta["rejected_check"]["missing"] == ["e1"]
    payload = build_payload(message, channel="hook", sent_at=T0)
    assert "rejected_text" not in payload["meta"] and "Power is fine" not in json.dumps(payload)


async def test_a_truncated_answer_is_kept_clipped(store):
    from argus_scout.narrative import MAX_REJECTED_CHARS

    seed_attention(store)
    long = "The UPS is on battery. " * 200
    report = await narrator(store, FakeGenerator(long, finish_reason="length", completion_tokens=400)).report("status")
    meta = report.meta
    assert meta["rejected_check"]["rule"] == "token_limit" and meta["rejected_check"]["missing"] == []
    assert len(meta["rejected_text"]) == MAX_REJECTED_CHARS and meta["rejected_text"].endswith("…")


async def test_an_accepted_answer_has_no_rejection_meta(store):
    seed_attention(store)
    report = await narrator(store, FakeGenerator(WHOLE)).report("morning")
    assert report.meta["path"] == "llm" and "rejected_text" not in report.meta and "rejected_check" not in report.meta


def test_clean_rejected_strips_control_and_format_characters():
    from argus_scout.narrative import clean_rejected

    assert clean_rejected("a\r\nb\x00c‮d\te\x07") == "a\nbcd\te"
    assert clean_rejected("x" * 50, limit=10) == "x" * 9 + "…"


# --- clipping at a word boundary ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "limit", "clipped"),
    [
        ("merge argus-safelist into main before the release", 20, "merge…"),
        ("merge the argus-safelist branch", 28, "merge the argus-safelist…"),
        ("The disk on the backup host is full. Free some space now", 40, "The disk on the backup host is full. …"),
        ("short enough", 40, "short enough"),
        ("a" * 50, 10, "a" * 9 + "…"),  # one word longer than the room: cut it
        ("ends exactly on a space here", 18, "ends exactly on a…"),
    ],
)
def test_clip_cuts_at_word_or_sentence_boundaries(text, limit, clipped):
    from argus_scout.narrative import _clip

    out = _clip(text, limit)
    assert out == clipped and len(out) <= limit


def test_long_fact_lines_never_end_mid_word():
    from argus_scout.narrative import MAX_LINE_CHARS, render_template

    summary = "Review queue: " + " ".join(f"merge argus-safelist-{i}" for i in range(40))
    text = render_template(Facts(kind="status", at=T0, groups=[_group("w1", "warning", summary)]), DEFAULT_PERSONA)
    [line] = [ln for ln in text.splitlines() if ln.startswith("- ")]
    assert len(line) - 2 <= MAX_LINE_CHARS and line.endswith("…")
    assert line.removesuffix("…").split()[-1] in summary.split()  # a whole word, not a fragment of one


# --- tariff wording -------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("currency", "rate", "shown"),
    [("AUD", 23.6916, "23.7 c/kWh"), ("USD", 48.0, "48 c/kWh"), ("JPY", 31.46, "31.5 JPY/kWh")],
)
async def test_prompt_and_template_show_rates_in_the_common_unit(store, tmp_path, currency, rate, shown):
    tariff = tmp_path / "tariff.yaml"
    tariff.write_text(
        f"plan: p\ncurrency: {currency}\nunit: kWh\nperiods:\n  all:\n    start: '00:00'\n    end: '00:00'\n"
        f"    rate: {rate}\n"
    )
    gen = FakeGenerator("x")
    report = await narrator(store, gen, tariff=TariffFile(tariff)).report("morning")
    assert f"all period now, {shown}, all day." in gen.prompts[0].user
    assert f"Energy: all period now, {shown}, all day." in report.text
    assert "minor units" not in gen.prompts[0].user + report.text
