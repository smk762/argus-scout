"""Text generation: the one way scout calls a language model.

Two backends, both :class:`argus_cortex.backends.RemoteBackend` subclasses, so the connection, the bearer
header, the timeout and the retry policy are cortex's, not scout's:

- :class:`OllamaBackend` posts to Ollama's ``/api/chat`` (non-streaming).
- :class:`OpenAICompatBackend` posts to an OpenAI-compatible ``/chat/completions``: a local server
  (vLLM, llama.cpp, LM Studio) or a hosted provider, whose key comes from ``services.llm.api_key_env``.

Cortex retries a POST only when the request never reached the server (connect errors, pool timeouts),
so a slow model is never asked twice, and wraps every failure (bad status, bad JSON, unreachable) in
:class:`~argus_cortex.backends.BackendError`.

:class:`TextGenerator` sits in front of a backend and is what the narrative calls:

``await generator.generate(prompt, max_tokens=..., kind="llm_calls")``

1. **Daily cap.** The call is charged to ``kind`` in the :class:`~argus_scout.budget.DailyBudget`
   *before* the model is asked, so a failing backend still counts and a retry loop can't run up an
   unbounded bill. Over the cap it raises :class:`LLMBudgetExceeded` and the model is never called.
2. **The call guard (the GPU-lease seam).** The model call runs inside ``call_guard()``, an async context
   manager that defaults to doing nothing. A GPU lease (``argus_cortex.gpu``, brokered by argus-iris)
   wraps the call here later: pass ``call_guard=lambda: lease(...)`` and the lease is held for exactly
   the duration of the HTTP round-trip, never while a prompt is being built or a report stored. A guard
   that raises (no lease granted) is reported as :class:`LLMUnavailable` like any backend failure.
3. **The call.** The backend's blocking HTTP call runs in a worker thread.

**The model's output is untrusted text.** :func:`clean_output` strips control characters and
``<think>`` blocks and caps its length; after that it is only ever stored and shown. Nothing parses it,
executes it or turns it into an action: a report that says "approve X" approves nothing.
"""

from __future__ import annotations

import asyncio
import re
import unicodedata
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager, nullcontext
from dataclasses import dataclass
from typing import Any

import structlog
from argus_cortex.backends import BackendError, RemoteBackend

from argus_scout.budget import DailyBudget
from argus_scout.config import LLMConfig, resolve_secret

log = structlog.get_logger()

LLM_CALLS = "llm_calls"
"""The budget kind every narrative model call is charged to (``budget.limits.llm_calls``)."""

CHARS_PER_TOKEN = 4
"""The rough estimate used everywhere a token count is needed without a tokenizer."""

_THINK = re.compile(r"<think>.*?(</think>|$)", re.DOTALL | re.IGNORECASE)


def estimate_tokens(text: str) -> int:
    """A tokenizer-free estimate: characters / 4, rounded up."""
    return -(-len(text) // CHARS_PER_TOKEN)


class LLMUnavailable(RuntimeError):
    """No text this time: the model is unreachable, failed, returned nothing, or wasn't allowed to run.

    ``reason`` is a short machine-readable tag recorded in a report's meta."""

    reason = "error"


class LLMBudgetExceeded(LLMUnavailable):
    """Today's cap for this kind is spent; the model was not called."""

    reason = "budget"


@dataclass(frozen=True)
class Prompt:
    """A system message (rules and persona) and a user message (the facts and the ask)."""

    system: str
    user: str

    @property
    def chars(self) -> int:
        return len(self.system) + len(self.user)


@dataclass(frozen=True)
class Completion:
    """What a model returned, already cleaned. ``text`` is untrusted: display it, never act on it."""

    text: str
    model: str
    backend: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


def clean_output(text: str, *, max_chars: int) -> str:
    """Model output made safe to store and display: no ``<think>`` blocks, no control characters (newlines
    and tabs kept), trimmed and capped at ``max_chars``. It stays plain text; nothing interprets it."""
    text = _THINK.sub("", text)
    text = "".join(ch for ch in text if ch in "\n\t" or unicodedata.category(ch)[0] != "C")
    text = text.strip()
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "…"
    return text


def _messages(prompt: Prompt) -> list[dict[str, str]]:
    messages = [{"role": "system", "content": prompt.system}] if prompt.system else []
    return [*messages, {"role": "user", "content": prompt.user}]


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


class TextBackend(RemoteBackend):
    """A remote chat model. Subclasses build the request and read the response for one wire format."""

    name = "text"
    path = ""

    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        api_key: str | None = None,
        timeout: float = 120.0,
        temperature: float | None = None,
    ) -> None:
        super().__init__(base_url, api_key=api_key, timeout=timeout)
        self.model = model
        self.temperature = temperature

    def request(self, prompt: Prompt, max_tokens: int) -> dict[str, Any]:  # pragma: no cover - abstract
        raise NotImplementedError

    def parse(self, data: dict[str, Any]) -> tuple[str, dict[str, Any]]:  # pragma: no cover - abstract
        """The response's text, and usage numbers for the report's meta."""
        raise NotImplementedError

    def complete(self, prompt: Prompt, *, max_tokens: int) -> Completion:
        """One blocking round-trip. Raises :class:`BackendError` on any failure, including an empty or
        malformed response."""
        data = self.post_json(self.path, self.request(prompt, max_tokens))
        if not isinstance(data, dict):
            raise BackendError(f"{self.name}: expected a JSON object, got {type(data).__name__}")
        try:
            text, usage = self.parse(data)
        except (KeyError, IndexError, TypeError) as exc:
            raise BackendError(f"{self.name}: unexpected response shape ({exc!r})") from exc
        if not isinstance(text, str):
            raise BackendError(f"{self.name}: response text is {type(text).__name__}, not a string")
        text = clean_output(text, max_chars=max_tokens * CHARS_PER_TOKEN * 2)
        if not text:
            raise BackendError(f"{self.name}: the model returned no text")
        return Completion(
            text=text,
            model=str(usage.get("model") or self.model),
            backend=self.name,
            prompt_tokens=_int(usage.get("prompt_tokens")),
            completion_tokens=_int(usage.get("completion_tokens")),
        )


class OllamaBackend(TextBackend):
    """Ollama's ``POST /api/chat`` with ``stream: false``."""

    name = "ollama"
    path = "/api/chat"

    def request(self, prompt: Prompt, max_tokens: int) -> dict[str, Any]:
        options: dict[str, Any] = {"num_predict": max_tokens}
        if self.temperature is not None:
            options["temperature"] = self.temperature
        return {"model": self.model, "messages": _messages(prompt), "stream": False, "options": options}

    def parse(self, data: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        if data.get("error"):
            raise BackendError(f"ollama: {data['error']}")
        usage = {
            "model": data.get("model"),
            "prompt_tokens": data.get("prompt_eval_count"),
            "completion_tokens": data.get("eval_count"),
        }
        return data["message"]["content"], usage


class OpenAICompatBackend(TextBackend):
    """An OpenAI-compatible ``POST {base_url}/chat/completions``. ``base_url`` includes the API prefix
    (``/v1``); a hosted provider's key is sent as a bearer token by cortex."""

    name = "openai"
    path = "chat/completions"  # relative, so it joins onto a base_url that ends in /v1

    def request(self, prompt: Prompt, max_tokens: int) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": _messages(prompt),
            "max_tokens": max_tokens,
            "stream": False,
        }
        if self.temperature is not None:
            body["temperature"] = self.temperature
        return body

    def parse(self, data: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        if data.get("error"):
            raise BackendError(f"openai: {data['error']}")
        usage = data.get("usage") or {}
        return data["choices"][0]["message"]["content"], {
            "model": data.get("model"),
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
        }


BACKENDS: dict[str, type[TextBackend]] = {"ollama": OllamaBackend, "openai": OpenAICompatBackend}


def build_backend(config: LLMConfig) -> TextBackend:
    """The backend ``services.llm`` names. The API key is read from ``api_key_env`` now, never stored in config."""
    secret = resolve_secret(config.api_key_env)
    return BACKENDS[config.backend](
        config.base_url,
        config.model,
        api_key=secret.get_secret_value() if secret else None,
        timeout=config.timeout,
        temperature=config.temperature,
    )


CallGuard = Callable[[], AbstractAsyncContextManager[Any]]


class TextGenerator:
    """Budgeted, guarded access to one text backend. See the module docstring for the order of checks."""

    def __init__(
        self,
        backend: TextBackend,
        budget: DailyBudget | None = None,
        *,
        call_guard: CallGuard | None = None,
    ) -> None:
        self.backend = backend
        self.budget = budget
        self.call_guard: CallGuard = call_guard or nullcontext

    @property
    def model(self) -> str:
        return self.backend.model

    async def generate(self, prompt: Prompt, *, max_tokens: int, kind: str = LLM_CALLS) -> Completion:
        """Generate text for ``prompt``: charge ``kind``, hold the call guard, call the model.

        Raises :class:`LLMBudgetExceeded` over the cap (the model is not called) and :class:`LLMUnavailable`
        for anything else that leaves no text. The returned text is untrusted.
        """
        if self.budget is not None and not self.budget.try_consume(kind):
            raise LLMBudgetExceeded(f"daily {kind} limit reached ({self.budget.limit_for(kind)})")
        try:
            async with self.call_guard():
                return await asyncio.to_thread(self.backend.complete, prompt, max_tokens=max_tokens)
        except BackendError as exc:
            log.warning("llm.failed", backend=self.backend.name, model=self.backend.model, error=str(exc))
            raise LLMUnavailable(str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 — a guard or transport bug degrades to the template, never crashes a job
            log.warning("llm.failed", backend=self.backend.name, model=self.backend.model, error=repr(exc))
            raise LLMUnavailable(repr(exc)) from exc

    def close(self) -> None:
        self.backend.unload()


__all__ = [
    "BACKENDS",
    "CHARS_PER_TOKEN",
    "LLM_CALLS",
    "CallGuard",
    "Completion",
    "LLMBudgetExceeded",
    "LLMUnavailable",
    "OllamaBackend",
    "OpenAICompatBackend",
    "Prompt",
    "TextBackend",
    "TextGenerator",
    "build_backend",
    "clean_output",
    "estimate_tokens",
]
