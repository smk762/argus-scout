"""GPU leases (argus-iris broker): the committed manifest, what a model call leases, where it goes, and what happens
when no lease is granted."""

from __future__ import annotations

import ast
import contextlib
from pathlib import Path

import httpx
import pytest
from argus_cortex.gpu import GpuLease, GpuLeaseRefused, GpuLeaseTimeout, NullCoordinator, coordinator_from_env
from argus_cortex.gpu_wire import load_tenant_manifest
from pydantic import ValidationError
from test_llm import PROMPT, ollama_reply, openai_reply, stub
from test_narrative import narrator, seed

from argus_scout import gpu
from argus_scout.budget import DailyBudget
from argus_scout.config import LLMConfig, LLMGpuConfig
from argus_scout.llm import LLM_CALLS, LLMGpuUnavailable, OllamaBackend, OpenAICompatBackend, TextGenerator
from argus_scout.narrative import build_narrator

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "gpu-tenant.yaml"
SRC = ROOT / "src" / "argus_scout"


class Recording:
    """A coordinator that grants at once (or raises) and keeps every lease's arguments and lifetime."""

    name = "rec"

    def __init__(self, base_url: str | None = None, raises: Exception | None = None, log: list | None = None) -> None:
        self.base_url, self.raises = base_url, raises
        self.calls: list[dict] = []
        self.log = log if log is not None else []

    @contextlib.asynccontextmanager
    async def alease(self, **kw):
        self.calls.append(kw)
        if self.raises is not None:
            raise self.raises
        self.log.append("acquire")
        try:
            yield GpuLease(via="broker", base_url=self.base_url)
        finally:
            self.log.append("release")

    def lease(self, **kw):  # pragma: no cover - scout leases from async code only
        raise NotImplementedError


def ollama_llm(**gpu_cfg) -> LLMConfig:
    return LLMConfig(backend="ollama", base_url="http://ollama:11434", model="small:8b", gpu=LLMGpuConfig(**gpu_cfg))


# --- the manifest ----------------------------------------------------------------------------------------------


def test_manifest_validates_and_matches_the_lease_defaults():
    manifest = load_tenant_manifest(MANIFEST)
    assert manifest.tenant == gpu.TENANT
    assert manifest.residents == ()  # the model server holds the weights, not scout
    (kind,) = manifest.kinds
    assert kind.kind == gpu.KIND
    assert (kind.vram_mb, kind.model_mb) == (gpu.DEFAULTS.min_vram_mb, gpu.DEFAULTS.model_mb)
    assert kind.priority == gpu.DEFAULTS.priority == "background"
    assert kind.preemptible is gpu.PREEMPTIBLE is True
    assert kind.target == "ollama"


def test_broker_coordinator_from_env_reads_the_manifest(monkeypatch):
    from argus_cortex.gpu_broker import BrokerCoordinator

    monkeypatch.setenv("ARGUS_GPU_COORDINATOR", "broker")
    monkeypatch.setenv("ARGUS_GPU_BROKER_URL", "http://broker.invalid:8120")
    monkeypatch.setenv("ARGUS_GPU_TENANT_MANIFEST", str(MANIFEST))
    monkeypatch.delenv("ARGUS_GPU_TENANT", raising=False)
    coord = coordinator_from_env()
    assert isinstance(coord, BrokerCoordinator) and coord.tenant == gpu.TENANT


def test_unset_environment_is_no_gating(monkeypatch):
    monkeypatch.delenv("ARGUS_GPU_COORDINATOR", raising=False)
    assert isinstance(gpu.default_coordinator(), NullCoordinator)


# --- config ----------------------------------------------------------------------------------------------------


def test_gpu_config_validation():
    with pytest.raises(ValidationError, match="model_mb"):
        LLMGpuConfig(min_vram_mb=2000, model_mb=3000)
    with pytest.raises(ValidationError, match="gpu.target"):
        LLMConfig(backend="openai", base_url="http://llm:8000/v1", model="m", gpu={})
    with pytest.raises(ValidationError, match="service"):
        LLMGpuConfig(target="Not A Target")
    cfg = LLMConfig(backend="openai", base_url="http://llm:8000/v1", model="m", gpu={"target": "vllm:m"})
    assert gpu.lease_target(cfg) == "vllm:m"


# --- what a call leases ----------------------------------------------------------------------------------------


def test_no_lease_without_gpu_config():
    assert gpu.narrative_guard(LLMConfig(backend="ollama", base_url="http://o:11434", model="m"), Recording()) is None


async def test_model_call_takes_one_preemptible_low_priority_lease():
    coord = Recording()
    llm = ollama_llm(min_vram_mb=4000, model_mb=3500)
    backend = stub(OllamaBackend("http://ollama:11434", "small:8b"), lambda r: ollama_reply(), [])
    await TextGenerator(backend, call_guard=gpu.narrative_guard(llm, coord)).generate(PROMPT, max_tokens=10)
    (call,) = coord.calls
    assert call["kind"] == "narrative" and call["preemptible"] is True and call["priority"] == "background"
    assert call["target"] == "ollama:small:8b"
    assert (call["min_vram_mb"], call["model_mb"]) == (4000, 3500)


async def test_lease_is_released_before_generate_returns():
    """Held for the model call only: released before the caller can go on to deliver (speak through echo)."""
    log: list[str] = []
    coord = Recording(log=log)

    def handler(request: httpx.Request) -> httpx.Response:
        log.append("call")
        return ollama_reply()

    backend = stub(OllamaBackend("http://ollama:11434", "small:8b"), handler, [])
    generator = TextGenerator(backend, call_guard=gpu.narrative_guard(ollama_llm(), coord))
    await generator.generate(PROMPT, max_tokens=10)
    log.append("returned")
    assert log == ["acquire", "call", "release", "returned"]


def test_only_the_narrative_guard_takes_a_lease():
    """Scout leases only around its own model call; nothing (channels, the echo synthesiser) leases around speech."""
    offenders = []
    for path in SRC.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            leases = (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("lease", "alease")
            )
            if leases and path.name != "gpu.py":
                offenders.append(f"{path.relative_to(SRC)}:{node.lineno}")
    assert offenders == []


# --- where the call goes ---------------------------------------------------------------------------------------


def test_routed_url():
    assert gpu.routed_url("http://ollama:11434", None) == "http://ollama:11434"
    assert gpu.routed_url("http://ollama:11434", "http://node-b:11434/") == "http://node-b:11434"
    assert gpu.routed_url("http://llm:8000/v1", "http://node-b:8000") == "http://node-b:8000/v1"
    assert gpu.routed_url("http://llm:8000/v1", "https://node-b/api/v1") == "https://node-b/api/v1"


async def test_call_goes_to_the_node_the_broker_chose():
    seen: list[httpx.Request] = []
    coord = Recording(base_url="http://node-b:11434")
    backend = OllamaBackend("http://ollama:11434", "small:8b")
    generator = TextGenerator(backend, call_guard=gpu.narrative_guard(ollama_llm(), coord))
    routed = stub(backend.at("http://node-b:11434"), lambda r: ollama_reply(), seen)
    generator._routed["http://node-b:11434"] = routed
    await generator.generate(PROMPT, max_tokens=10)
    assert [str(r.url) for r in seen] == ["http://node-b:11434/api/chat"]
    generator.close()


def test_routed_backend_keeps_model_and_key():
    backend = OpenAICompatBackend("http://llm:8000/v1", "m", api_key="k", timeout=9, temperature=0.2)
    moved = backend.at("http://node-b:8000/v1")
    assert (moved.base_url, moved.model, moved.api_key, moved.timeout, moved.temperature) == (
        "http://node-b:8000/v1", "m", "k", 9, 0.2,
    )  # fmt: skip
    assert backend.at("http://llm:8000/v1/") is backend


async def test_no_broker_url_uses_the_configured_one():
    seen: list[httpx.Request] = []
    backend = stub(OpenAICompatBackend("http://llm:8000/v1", "m"), lambda r: openai_reply(), seen)
    llm = LLMConfig(backend="openai", base_url="http://llm:8000/v1", model="m", gpu={"target": "vllm:m"})
    await TextGenerator(backend, call_guard=gpu.narrative_guard(llm, Recording())).generate(PROMPT, max_tokens=10)
    assert [str(r.url) for r in seen] == ["http://llm:8000/v1/chat/completions"]


# --- no lease in time ------------------------------------------------------------------------------------------


@pytest.mark.parametrize("error", [GpuLeaseTimeout("no slot in 300s"), GpuLeaseRefused("can never fit")])
async def test_no_lease_is_gpu_unavailable_and_not_charged(error):
    seen: list[httpx.Request] = []
    backend = stub(OllamaBackend("http://ollama:11434", "small:8b"), lambda r: ollama_reply(), seen)
    budget = DailyBudget({LLM_CALLS: 5})
    generator = TextGenerator(backend, budget, call_guard=gpu.narrative_guard(ollama_llm(), Recording(raises=error)))
    with pytest.raises(LLMGpuUnavailable) as caught:
        await generator.generate(PROMPT, max_tokens=10)
    assert caught.value.reason == "gpu_unavailable"
    assert seen == [] and budget.used(LLM_CALLS) == 0


async def test_over_cap_waits_for_no_lease():
    coord = Recording()
    backend = stub(OllamaBackend("http://ollama:11434", "small:8b"), lambda r: ollama_reply(), [])
    budget = DailyBudget({LLM_CALLS: 1})
    budget.consume(LLM_CALLS)
    generator = TextGenerator(backend, budget, call_guard=gpu.narrative_guard(ollama_llm(), coord))
    with pytest.raises(Exception, match="limit reached"):
        await generator.generate(PROMPT, max_tokens=10)
    assert coord.calls == []


async def test_briefing_without_a_lease_falls_back_to_the_template(tmp_path):
    """No GPU in time never skips a briefing: it is written from the template and stored."""
    from argus_scout.config import ScoutConfig
    from argus_scout.store import SqliteStore

    config = ScoutConfig.model_validate(
        {"store": {"path": str(tmp_path / "scout.db")}, "services": {"llm": ollama_llm().model_dump(mode="json")}}
    )
    with SqliteStore(tmp_path / "scout.db") as store:
        seed(store)
        n = build_narrator(config, store, None, coordinator=Recording(raises=GpuLeaseTimeout("no slot")))
        n.clock = narrator(store).clock
        report = await n.report("morning")
        n.generator.close()
        assert report.meta["path"] == "template" and report.meta["fallback_reason"] == "gpu_unavailable"
        assert "no GPU was free" in report.text and "UPS on battery" in report.text
        assert store.latest_report("morning").id == report.id
