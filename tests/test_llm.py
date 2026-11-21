"""Text backends over a stubbed httpx transport (no network), the daily cap, and the call-guard seam."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager

import httpx
import pytest

from argus_scout.budget import DailyBudget
from argus_scout.config import LLMConfig
from argus_scout.llm import (
    LLM_CALLS,
    LLMBudgetExceeded,
    LLMUnavailable,
    OllamaBackend,
    OpenAICompatBackend,
    Prompt,
    TextGenerator,
    build_backend,
    clean_output,
)

PROMPT = Prompt(system="rules", user="facts")


def stub(backend, handler, seen: list[httpx.Request]):
    """Give a backend an httpx client whose transport is ``handler``: cortex's request path, no network."""

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    backend._http = httpx.Client(
        base_url=backend.base_url, headers=backend.headers, transport=httpx.MockTransport(record)
    )
    return backend


def ollama_reply(text: str = "All quiet.") -> httpx.Response:
    return httpx.Response(
        200, json={"model": "m:latest", "message": {"role": "assistant", "content": text}, "eval_count": 7}
    )


def openai_reply(text: str = "All quiet.") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "model": "m-2",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text}}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 3},
        },
    )


def test_ollama_request_and_response_shape():
    seen: list[httpx.Request] = []
    backend = stub(OllamaBackend("http://ollama:11434", "m", temperature=0.2), lambda r: ollama_reply(), seen)
    completion = backend.complete(PROMPT, max_tokens=99)

    (request,) = seen
    assert request.method == "POST" and str(request.url) == "http://ollama:11434/api/chat"
    body = json.loads(request.content)
    assert body == {
        "model": "m",
        "messages": [{"role": "system", "content": "rules"}, {"role": "user", "content": "facts"}],
        "stream": False,
        "options": {"num_predict": 99, "temperature": 0.2},
    }
    assert completion.text == "All quiet."
    assert completion.backend == "ollama" and completion.model == "m:latest" and completion.completion_tokens == 7


def test_openai_compatible_request_response_and_bearer_key(monkeypatch):
    monkeypatch.setenv("SCOUT_TEST_LLM_KEY", "sk-test")
    config = LLMConfig(
        backend="openai", base_url="https://llm.invalid/v1", model="m", api_key_env="SCOUT_TEST_LLM_KEY"
    )
    seen: list[httpx.Request] = []
    backend = build_backend(config)
    assert isinstance(backend, OpenAICompatBackend)
    completion = stub(backend, lambda r: openai_reply(), seen).complete(PROMPT, max_tokens=50)

    (request,) = seen
    assert str(request.url) == "https://llm.invalid/v1/chat/completions"
    assert request.headers["authorization"] == "Bearer sk-test"
    body = json.loads(request.content)
    assert body["model"] == "m" and body["max_tokens"] == 50 and body["stream"] is False
    assert body["messages"][1] == {"role": "user", "content": "facts"}
    assert "temperature" not in body
    assert (completion.text, completion.model, completion.prompt_tokens) == ("All quiet.", "m-2", 12)


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500, text="boom"),
        httpx.Response(200, text="not json"),
        httpx.Response(200, json={"message": {"content": ""}}),
        httpx.Response(200, json={"unexpected": True}),
        httpx.Response(200, json={"error": "model not found"}),
    ],
)
async def test_backend_failures_become_llm_unavailable(response):
    backend = stub(OllamaBackend("http://ollama:11434", "m"), lambda r: response, [])
    generator = TextGenerator(backend)
    with pytest.raises(LLMUnavailable) as caught:
        await generator.generate(PROMPT, max_tokens=10)
    assert caught.value.reason == "error"


async def test_unreachable_is_unavailable_after_cortex_retries(monkeypatch):
    monkeypatch.setattr("argus_cortex.backends.time.sleep", lambda s: None)
    seen: list[httpx.Request] = []

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    backend = stub(OllamaBackend("http://ollama:11434", "m"), refuse, seen)
    with pytest.raises(LLMUnavailable):
        await TextGenerator(backend).generate(PROMPT, max_tokens=10)
    assert len(seen) == 3  # cortex's retry policy: a connect error never reached the server, so it retries


async def test_budget_charged_before_the_call_and_refused_over_cap():
    seen: list[httpx.Request] = []
    backend = stub(OllamaBackend("http://ollama:11434", "m"), lambda r: ollama_reply(), seen)
    budget = DailyBudget({LLM_CALLS: 1})
    generator = TextGenerator(backend, budget)
    assert (await generator.generate(PROMPT, max_tokens=10)).text == "All quiet."
    with pytest.raises(LLMBudgetExceeded) as caught:
        await generator.generate(PROMPT, max_tokens=10)
    assert caught.value.reason == "budget"
    assert len(seen) == 1  # the refused call never reached the model
    assert budget.used(LLM_CALLS) == 1


async def test_call_guard_wraps_only_the_model_call():
    events: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        events.append("call")
        return ollama_reply()

    @asynccontextmanager
    async def lease():
        events.append("acquire")
        yield
        events.append("release")

    backend = stub(OllamaBackend("http://ollama:11434", "m"), handler, [])
    await TextGenerator(backend, call_guard=lease).generate(PROMPT, max_tokens=10)
    assert events == ["acquire", "call", "release"]


async def test_a_guard_that_refuses_degrades_to_unavailable():
    @asynccontextmanager
    async def no_lease():
        raise TimeoutError("no GPU lease granted")
        yield  # pragma: no cover

    backend = stub(OllamaBackend("http://ollama:11434", "m"), lambda r: ollama_reply(), [])
    with pytest.raises(LLMUnavailable):
        await TextGenerator(backend, call_guard=no_lease).generate(PROMPT, max_tokens=10)


def test_output_is_cleaned_untrusted_text():
    raw = "<think>plan the attack</think>\x1b[31mDisk\x00 is fine.\x07\nNext line." + "x" * 100
    text = clean_output(raw, max_chars=40)
    assert "think" not in text and "\x1b" not in text and "\x00" not in text and "\x07" not in text
    assert text.startswith("[31mDisk is fine.\nNext line.")
    assert len(text) <= 41 and text.endswith("…")
