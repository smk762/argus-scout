"""Text backends over a stubbed httpx transport (no network), the daily cap, and the call-guard seam."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager

import httpx
import pytest
from argus_cortex.backends import BackendError

from argus_scout.budget import DailyBudget
from argus_scout.config import LLMConfig
from argus_scout.llm import (
    LLM_CALLS,
    AnthropicBackend,
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
    config = LLMConfig(backend="openai", base_url="https://llm.invalid/v1", model="m", api_key_env="SCOUT_TEST_LLM_KEY")
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


# --- thinking and the finish reason -------------------------------------------------------------------------------


def ollama_config(**kw) -> LLMConfig:
    return LLMConfig(backend="ollama", base_url="http://ollama:11434", model="qwen3:8b", **kw)


def test_ollama_sends_think_false_by_default():
    seen: list[httpx.Request] = []
    stub(build_backend(ollama_config()), lambda r: ollama_reply(), seen).complete(PROMPT, max_tokens=50)
    assert json.loads(seen[0].content)["think"] is False


def test_ollama_omits_think_when_null_and_sends_true_when_asked():
    seen: list[httpx.Request] = []
    stub(build_backend(ollama_config(think=None)), lambda r: ollama_reply(), seen).complete(PROMPT, max_tokens=50)
    stub(build_backend(ollama_config(think=True)), lambda r: ollama_reply(), seen).complete(PROMPT, max_tokens=50)
    assert "think" not in json.loads(seen[0].content)
    assert json.loads(seen[1].content)["think"] is True


def test_ollama_that_refuses_think_is_asked_once_more_without_it():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if "think" in json.loads(request.content):
            return httpx.Response(400, json={"error": 'invalid field "think"'})
        return ollama_reply()

    backend = stub(build_backend(ollama_config()), handler, seen)
    assert backend.complete(PROMPT, max_tokens=50).text == "All quiet."
    assert ["think" in json.loads(r.content) for r in seen] == [True, False]
    backend.complete(PROMPT, max_tokens=50)  # remembered: not sent again
    assert "think" not in json.loads(seen[-1].content) and len(seen) == 3


def test_other_ollama_400s_are_not_retried():
    seen: list[httpx.Request] = []
    backend = stub(build_backend(ollama_config()), lambda r: httpx.Response(400, json={"error": "bad model"}), seen)
    with pytest.raises(BackendError):
        backend.complete(PROMPT, max_tokens=50)
    assert len(seen) == 1


def test_openai_sends_no_think_field_and_passes_extra_body_through():
    seen: list[httpx.Request] = []
    plain = LLMConfig(backend="openai", base_url="http://llm:8000/v1", model="m")
    stub(build_backend(plain), lambda r: openai_reply(), seen).complete(PROMPT, max_tokens=50)
    body = json.loads(seen[0].content)
    assert not {"think", "chat_template_kwargs", "reasoning_effort"} & set(body)

    extra = LLMConfig(backend="openai", base_url="http://llm:8000/v1", model="m",
                      extra_body={"chat_template_kwargs": {"enable_thinking": False}})  # fmt: skip
    stub(build_backend(extra), lambda r: openai_reply(), seen).complete(PROMPT, max_tokens=50)
    body = json.loads(seen[1].content)
    assert body["chat_template_kwargs"] == {"enable_thinking": False} and body["max_tokens"] == 50


def test_extra_body_is_openai_only_and_cannot_replace_scouts_fields():
    with pytest.raises(ValueError, match="openai"):
        ollama_config(extra_body={"x": 1})
    with pytest.raises(ValueError, match="max_tokens"):
        LLMConfig(backend="openai", base_url="http://llm:8000/v1", model="m", extra_body={"max_tokens": 9})


def test_finish_reason_is_read_from_both_backends():
    cut = httpx.Response(200, json={"message": {"content": "Cut o"}, "done_reason": "length", "eval_count": 50})
    completion = stub(OllamaBackend("http://ollama:11434", "m"), lambda r: cut, []).complete(PROMPT, max_tokens=50)
    assert completion.truncated and completion.completion_tokens == 50

    reply = json.loads(openai_reply().content)
    reply["choices"][0]["finish_reason"] = "stop"
    openai = stub(OpenAICompatBackend("http://llm:8000/v1", "m"), lambda r: httpx.Response(200, json=reply), [])
    completion = openai.complete(PROMPT, max_tokens=50)
    assert completion.finish_reason == "stop" and not completion.truncated


@pytest.mark.parametrize(
    "message",
    [{"content": "<think>the facts are"}, {"content": "", "thinking": "the facts are"}],
    ids=["think-in-content", "think-field"],
)
def test_a_budget_spent_thinking_comes_back_empty_and_truncated(message):
    """Not an error: the narrative must see that it was cut off, to say so."""
    response = httpx.Response(200, json={"message": message, "done_reason": "length", "eval_count": 400})
    backend = stub(OllamaBackend("http://ollama:11434", "m"), lambda r: response, [])
    completion = backend.complete(PROMPT, max_tokens=400)
    assert completion.text == "" and completion.truncated and completion.completion_tokens == 400


def test_a_think_only_answer_comes_back_empty_not_as_an_error():
    response = httpx.Response(200, json={"message": {"content": "<think>hm</think>"}, "done_reason": "stop"})
    completion = stub(OllamaBackend("http://ollama:11434", "m"), lambda r: response, []).complete(PROMPT, max_tokens=50)
    assert completion.text == "" and not completion.truncated


def test_routed_backend_keeps_think_and_extra_body():
    assert OllamaBackend("http://a:11434", "m", think=False).at("http://b:11434").think is False
    openai = OpenAICompatBackend("http://a:8000/v1", "m", extra_body={"reasoning_effort": "low"})
    assert openai.at("http://b:8000/v1").extra_body == {"reasoning_effort": "low"}


def test_max_tokens_defaults_per_kind_and_is_bounded_by_the_context():
    config = ollama_config()
    assert [config.max_tokens_for(k) for k in ("morning", "evening", "status")] == [800, 800, 400]
    assert ollama_config(context_tokens=1024).max_tokens_for("morning") == 512
    assert ollama_config(max_tokens=300).max_tokens_for("morning") == 300
    with pytest.raises(ValueError, match="half of context_tokens"):
        ollama_config(max_tokens=2000)


def anthropic_reply(text: str = "All quiet.", stop: str = "end_turn", **extra) -> httpx.Response:
    content = [{"type": "thinking", "thinking": "", "signature": "x"}, {"type": "text", "text": text}]
    return httpx.Response(
        200,
        json={"type": "message", "model": "claude-opus-5-5", "content": content, "stop_reason": stop,
              "usage": {"input_tokens": 120, "output_tokens": 40}, **extra},
    )  # fmt: skip


def anthropic_config(**kw) -> LLMConfig:
    base = {"backend": "anthropic", "base_url": "https://api.anthropic.invalid/v1", "model": "claude-opus-5-5",
            "api_key_env": "SCOUT_TEST_ANTHROPIC_KEY"}  # fmt: skip
    return LLMConfig(**(base | kw))


def test_anthropic_request_headers_and_text_blocks_only(monkeypatch):
    monkeypatch.setenv("SCOUT_TEST_ANTHROPIC_KEY", "sk-ant-test")
    seen: list[httpx.Request] = []
    backend = build_backend(anthropic_config(effort="low"))
    assert isinstance(backend, AnthropicBackend)
    completion = stub(backend, lambda r: anthropic_reply(), seen).complete(PROMPT, max_tokens=4000)

    (request,) = seen
    assert str(request.url) == "https://api.anthropic.invalid/v1/messages"
    assert request.headers["x-api-key"] == "sk-ant-test"
    assert "authorization" not in request.headers  # never a bearer token
    assert request.headers["anthropic-version"] == "2026-10-21"
    assert request.headers["anthropic-beta"] == "server-side-fallback-2026-07-01"
    body = json.loads(request.content)
    assert body["system"] == "rules" and body["messages"] == [{"role": "user", "content": "facts"}]
    assert body["max_tokens"] == 4000 and body["output_config"] == {"effort": "low"}
    assert body["fallbacks"] == "default"
    assert not {"temperature", "top_p", "top_k", "thinking", "stream"} & set(body)
    assert (completion.text, completion.model, completion.prompt_tokens, completion.completion_tokens) == (
        "All quiet.", "claude-opus-5-5", 120, 40)  # fmt: skip
    assert completion.finish_reason == "stop" and not completion.truncated


def test_anthropic_max_tokens_is_a_truncation_and_a_refusal_is_a_failure(monkeypatch):
    monkeypatch.setenv("SCOUT_TEST_ANTHROPIC_KEY", "k")
    backend = stub(build_backend(anthropic_config()), lambda r: anthropic_reply("Half a sen", "max_tokens"), [])
    assert backend.complete(PROMPT, max_tokens=100).truncated
    refused = anthropic_reply("", "refusal", stop_details={"type": "refusal", "category": "cyber"})
    backend = stub(build_backend(anthropic_config()), lambda r: refused, [])
    with pytest.raises(BackendError, match="declined"):
        backend.complete(PROMPT, max_tokens=100)


async def test_anthropic_errors_become_llm_unavailable(monkeypatch):
    monkeypatch.setenv("SCOUT_TEST_ANTHROPIC_KEY", "k")
    error = httpx.Response(401, json={"type": "error", "error": {"type": "authentication_error"}})
    backend = stub(build_backend(anthropic_config()), lambda r: error, [])
    with pytest.raises(LLMUnavailable):
        await TextGenerator(backend).generate(PROMPT, max_tokens=100)


@pytest.mark.parametrize(
    "kw, message",
    [
        ({"api_key_env": None}, "needs api_key_env"),
        ({"temperature": 0.2}, "no temperature"),
        ({"gpu": {"min_vram_mb": 1000, "model_mb": 1000}}, "no GPU lease"),
        ({"extra_body": {"x": 1}}, "extra_body is for backend openai"),
    ],
)
def test_anthropic_config_refuses_what_the_api_would(kw, message):
    with pytest.raises(ValueError, match=message):
        anthropic_config(**kw)


def test_effort_is_anthropic_only():
    with pytest.raises(ValueError, match="effort is for backend anthropic"):
        LLMConfig(backend="ollama", base_url="http://ollama:11434", model="m", effort="low")
