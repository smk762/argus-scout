"""Text generation: the one way scout calls a language model.

Three backends, all :class:`argus_cortex.backends.RemoteBackend` subclasses, so the connection, the timeout and
the retry policy are cortex's, not scout's:

- :class:`OllamaBackend` posts to Ollama's ``/api/chat`` (non-streaming).
- :class:`OpenAICompatBackend` posts to an OpenAI-compatible ``/chat/completions``: a local server
  (vLLM, llama.cpp, LM Studio) or a hosted provider, whose key comes from ``services.llm.api_key_env``.
- :class:`AnthropicBackend` posts to the Claude Messages API (``{base_url}/messages``, ``base_url`` ending in
  ``/v1``) with ``x-api-key`` and ``anthropic-version``, never a bearer token. Claude models think before they
  answer (it can't be turned off on Claude Opus 5.5); ``services.llm.effort`` sets how much, and thinking is
  charged to ``max_tokens``, so give it room. Only ``text`` blocks are read: thinking and fallback blocks are
  skipped. ``stop_reason: max_tokens`` is a truncation (``length``); ``refusal`` is a failed call, so the report
  falls back to its template. Server-side refusal fallbacks are on (``fallbacks: "default"``).

Cortex retries a POST only when the request never reached the server (connect errors, pool timeouts),
so a slow model is never asked twice, and wraps every failure (bad status, bad JSON, unreachable) in
:class:`~argus_cortex.backends.BackendError`.

:class:`TextGenerator` sits in front of a backend and is what the narrative calls:

``await generator.generate(prompt, max_tokens=..., kind="llm_calls")``

1. **Daily cap, checked.** Over today's cap for ``kind`` it raises :class:`LLMBudgetExceeded` at once: no
   lease is waited for and the model is never called.
2. **The call guard (the GPU lease).** The rest runs inside ``call_guard()``, an async context manager that
   defaults to doing nothing. With ``services.llm.gpu`` set it is :func:`argus_scout.gpu.narrative_guard`: a
   preemptible ``argus_cortex.gpu`` lease (brokered by argus-iris) held for exactly the model call, never
   while a prompt is being built or a report stored. A lease that is not granted
   (:data:`argus_scout.gpu.LEASE_ERRORS`) raises :class:`LLMGpuUnavailable`; any other guard failure is
   :class:`LLMUnavailable`. If the guard yields a grant with a ``base_url`` (the broker placed the work on
   another node), the call goes to that model server (:func:`argus_scout.gpu.routed_url`).
3. **Daily cap, charged.** The call is charged to ``kind`` in the :class:`~argus_scout.budget.DailyBudget`
   *before* the model is asked, so a failing backend still counts and a retry loop can't run up an
   unbounded bill. The charge is atomic across processes (see :mod:`argus_scout.budget`), so if another
   process spent the last call while this one waited for its lease, it is refused here.
4. **The call.** The backend's blocking HTTP call runs in a worker thread.

Speech is argus-echo's GPU work, under echo's own lease and residency: nothing here wraps a call to echo
(see :mod:`argus_scout.gpu`).

**Thinking.** Reasoning models (qwen3, deepseek-r1) spend their output budget on a ``<think>`` block before
the answer. :class:`OllamaBackend` sends ``think`` (``services.llm.think``, default ``false``) so Ollama skips
it; an Ollama that refuses the field with a 400 naming it is asked once more without it, and the backend stops
sending it (``llm.think_unsupported`` is logged). Older Ollamas ignore unknown fields. OpenAI-compatible servers
disagree on the field (``reasoning_effort``, ``chat_template_kwargs``), so :class:`OpenAICompatBackend` sends
none; ``services.llm.extra_body`` passes one through for a server that needs it.

**Finish reason.** Every :class:`Completion` carries the server's finish reason (Ollama ``done_reason``,
OpenAI ``finish_reason``); ``"length"`` means the token limit cut the answer off
(:attr:`Completion.truncated`). A truncated completion is returned even when nothing is left after cleaning
(the whole budget went on thinking), and so is one whose text was all ``<think>`` block, so the narrative can
tell a cut-off or hollow answer from a failed call and fall back to its template (:mod:`argus_scout.narrative`).

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
from argus_scout.gpu import LEASE_ERRORS, routed_url

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


class LLMGpuUnavailable(LLMUnavailable):
    """No GPU lease was granted in time (or the broker refused it); the model was not called or charged."""

    reason = "gpu_unavailable"


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
    finish_reason: str | None = None
    """Why the model stopped, as the server said it (``stop``, ``length``); ``None`` if it didn't say."""

    @property
    def truncated(self) -> bool:
        """The token limit cut the answer off: it may be missing anything after the cut."""
        return self.finish_reason == "length"


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
        think: bool | None = None,
        extra_body: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(base_url, api_key=api_key, timeout=timeout)
        self.model = model
        self.temperature = temperature
        self.think = think
        self.extra_body = dict(extra_body or {})

    def at(self, base_url: str) -> TextBackend:
        """This backend (same model, key, timeout, temperature, think and extra body) pointed at ``base_url``;
        itself if unchanged."""
        if base_url.rstrip("/") == self.base_url:
            return self
        return type(self)(
            base_url,
            self.model,
            api_key=self.api_key,
            timeout=self.timeout,
            temperature=self.temperature,
            think=self.think,
            extra_body=self.extra_body,
        )

    def request(self, prompt: Prompt, max_tokens: int) -> dict[str, Any]:  # pragma: no cover - abstract
        raise NotImplementedError

    def parse(self, data: dict[str, Any]) -> tuple[str, dict[str, Any]]:  # pragma: no cover - abstract
        """The response's text, and usage numbers for the report's meta."""
        raise NotImplementedError

    def send(self, prompt: Prompt, max_tokens: int) -> Any:
        """POST the request and return the decoded JSON. Raises :class:`BackendError`."""
        return self.post_json(self.path, self.request(prompt, max_tokens))

    def complete(self, prompt: Prompt, *, max_tokens: int) -> Completion:
        """One blocking round-trip. Raises :class:`BackendError` on any failure, including a malformed response
        or an empty one that the token limit didn't cut off. A completion whose text cleans to nothing (all
        ``<think>``) or that was truncated comes back with ``text`` possibly empty: the caller judges it."""
        data = self.send(prompt, max_tokens)
        if not isinstance(data, dict):
            raise BackendError(f"{self.name}: expected a JSON object, got {type(data).__name__}")
        try:
            text, usage = self.parse(data)
        except (KeyError, IndexError, TypeError) as exc:
            raise BackendError(f"{self.name}: unexpected response shape ({exc!r})") from exc
        if not isinstance(text, str):
            raise BackendError(f"{self.name}: response text is {type(text).__name__}, not a string")
        finish = usage.get("finish_reason")
        finish_reason = finish if isinstance(finish, str) and finish else None
        if not text.strip() and finish_reason != "length":
            raise BackendError(f"{self.name}: the model returned no text")
        return Completion(
            text=clean_output(text, max_chars=max_tokens * CHARS_PER_TOKEN * 2),
            model=str(usage.get("model") or self.model),
            backend=self.name,
            prompt_tokens=_int(usage.get("prompt_tokens")),
            completion_tokens=_int(usage.get("completion_tokens")),
            finish_reason=finish_reason,
        )


def _refuses_think(exc: BackendError) -> bool:
    """The server answered 400 and its error names ``think``: an Ollama that won't take the field."""
    response = getattr(exc.__cause__, "response", None)
    if getattr(response, "status_code", None) != 400:
        return False
    try:
        return "think" in str(response.text).lower()  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001 — an unreadable body is just "not that error"
        return False


class OllamaBackend(TextBackend):
    """Ollama's ``POST /api/chat`` with ``stream: false``, and ``think`` when it isn't ``None``."""

    name = "ollama"
    path = "/api/chat"
    think_refused = False
    """Set once this server refused ``think``: it isn't sent again."""

    def request(self, prompt: Prompt, max_tokens: int) -> dict[str, Any]:
        options: dict[str, Any] = {"num_predict": max_tokens}
        if self.temperature is not None:
            options["temperature"] = self.temperature
        body: dict[str, Any] = {"model": self.model, "messages": _messages(prompt), "stream": False}
        if self.think is not None and not self.think_refused:
            body["think"] = self.think
        return body | {"options": options}

    def send(self, prompt: Prompt, max_tokens: int) -> Any:
        body = self.request(prompt, max_tokens)
        try:
            return self.post_json(self.path, body)
        except BackendError as exc:
            if "think" not in body or not _refuses_think(exc):
                raise
            log.warning("llm.think_unsupported", backend=self.name, model=self.model, error=str(exc))
            self.think_refused = True
            del body["think"]
            return self.post_json(self.path, body)

    def parse(self, data: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        if data.get("error"):
            raise BackendError(f"ollama: {data['error']}")
        usage = {
            "model": data.get("model"),
            "prompt_tokens": data.get("prompt_eval_count"),
            "completion_tokens": data.get("eval_count"),
            "finish_reason": data.get("done_reason"),
        }
        return data["message"]["content"] or "", usage


class OpenAICompatBackend(TextBackend):
    """An OpenAI-compatible ``POST {base_url}/chat/completions``. ``base_url`` includes the API prefix
    (``/v1``); a hosted provider's key is sent as a bearer token by cortex.

    It sends no thinking switch of its own (servers disagree on its name); ``extra_body`` is merged into the
    request for a server that needs one (``chat_template_kwargs``, ``reasoning_effort``). It never replaces
    the fields scout sets."""

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
        return {**self.extra_body, **body}

    def parse(self, data: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        if data.get("error"):
            raise BackendError(f"openai: {data['error']}")
        usage = data.get("usage") or {}
        choice = data["choices"][0]
        return choice["message"]["content"] or "", {
            "model": data.get("model"),
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "finish_reason": choice.get("finish_reason"),
        }


ANTHROPIC_VERSION = "2026-10-21"
"""The Claude API version scout's requests are written for (no ``temperature``, ``top_p`` or ``top_k``)."""
ANTHROPIC_FALLBACK_BETA = "server-side-fallback-2026-07-01"
"""Lets a request declined by the model's safety classifiers be re-run on a fallback model in the same call."""


class AnthropicBackend(TextBackend):
    """The Claude Messages API, ``POST {base_url}/messages``, with ``base_url`` ending in ``/v1``
    (``https://api.anthropic.com/v1``). The key goes in ``x-api-key``, not a bearer header. ``effort``
    (``output_config.effort``) sets how much the model thinks; ``temperature`` is never sent (refused by the API
    version used). ``fallbacks: "default"`` asks the API to re-run a request its classifiers decline on a
    fallback model; a refusal that survives that is a :class:`BackendError`."""

    name = "anthropic"
    path = "messages"  # relative, so it joins onto a base_url that ends in /v1

    def __init__(
        self, base_url: str, model: str, *, api_key: str | None = None, effort: str | None = None, **kwargs: Any
    ) -> None:
        super().__init__(base_url, model, api_key=None, **kwargs)
        self.api_key = api_key  # kept for at(); sent as x-api-key, never as a bearer token
        self.effort = effort
        self.headers = {
            "anthropic-version": ANTHROPIC_VERSION,
            "anthropic-beta": ANTHROPIC_FALLBACK_BETA,
            **({"x-api-key": api_key} if api_key else {}),
        }

    def at(self, base_url: str) -> TextBackend:
        if base_url.rstrip("/") == self.base_url:
            return self
        return type(self)(base_url, self.model, api_key=self.api_key, timeout=self.timeout, effort=self.effort)

    def request(self, prompt: Prompt, max_tokens: int) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt.user}],
            "fallbacks": "default",
        }
        if prompt.system:
            body["system"] = prompt.system
        if self.effort:
            body["output_config"] = {"effort": self.effort}
        return body

    def parse(self, data: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        if data.get("type") == "error" or data.get("error"):
            raise BackendError(f"anthropic: {data.get('error')}")
        stop = data.get("stop_reason")
        if stop == "refusal":
            details = data.get("stop_details") or {}
            raise BackendError(f"anthropic: the model declined (category {details.get('category')!r})")
        text = "".join(b["text"] for b in data["content"] if isinstance(b, dict) and b.get("type") == "text")
        usage = data.get("usage") or {}
        return text, {
            "model": data.get("model"),
            "prompt_tokens": usage.get("input_tokens"),
            "completion_tokens": usage.get("output_tokens"),
            "finish_reason": "length" if stop == "max_tokens" else ("stop" if stop == "end_turn" else stop),
        }


BACKENDS: dict[str, type[TextBackend]] = {
    "ollama": OllamaBackend,
    "openai": OpenAICompatBackend,
    "anthropic": AnthropicBackend,
}


def build_backend(config: LLMConfig) -> TextBackend:
    """The backend ``services.llm`` names. The API key is read from ``api_key_env`` now, never stored in config."""
    secret = resolve_secret(config.api_key_env)
    key = secret.get_secret_value() if secret else None
    if config.backend == "anthropic":
        return AnthropicBackend(
            str(config.base_url), config.model, api_key=key, timeout=config.timeout, effort=config.effort
        )
    return BACKENDS[config.backend](
        config.base_url,
        config.model,
        api_key=key,
        timeout=config.timeout,
        temperature=config.temperature,
        think=config.think if config.backend == "ollama" else None,
        extra_body=config.extra_body,
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
        self._routed: dict[str, TextBackend] = {}

    @property
    def model(self) -> str:
        return self.backend.model

    async def generate(self, prompt: Prompt, *, max_tokens: int, kind: str = LLM_CALLS) -> Completion:
        """Generate text for ``prompt``: check the cap, hold the call guard, charge ``kind``, call the model.

        Raises :class:`LLMBudgetExceeded` over the cap (the model is not called), :class:`LLMGpuUnavailable` when
        no GPU lease was granted, and :class:`LLMUnavailable` for anything else that leaves no text. The returned
        text is untrusted.
        """
        if self.budget is not None and not self.budget.available(kind):
            raise self._over_cap(kind)
        try:
            async with self.call_guard() as grant:
                if self.budget is not None and not self.budget.try_consume(kind):
                    raise self._over_cap(kind)
                backend = self._backend_for(getattr(grant, "base_url", None))
                return await asyncio.to_thread(backend.complete, prompt, max_tokens=max_tokens)
        except LLMBudgetExceeded:
            raise
        except LEASE_ERRORS as exc:
            log.warning("llm.no_gpu_lease", backend=self.backend.name, model=self.backend.model, error=str(exc))
            raise LLMGpuUnavailable(str(exc)) from exc
        except BackendError as exc:
            log.warning("llm.failed", backend=self.backend.name, model=self.backend.model, error=str(exc))
            raise LLMUnavailable(str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 — a guard or transport bug degrades to the template, never crashes a job
            log.warning("llm.failed", backend=self.backend.name, model=self.backend.model, error=repr(exc))
            raise LLMUnavailable(repr(exc)) from exc

    def _over_cap(self, kind: str) -> LLMBudgetExceeded:
        assert self.budget is not None
        return LLMBudgetExceeded(f"daily {kind} limit reached ({self.budget.limit_for(kind)})")

    def _backend_for(self, granted_url: str | None) -> TextBackend:
        """The configured backend, or one pointed at the model server the broker's grant named."""
        if not granted_url or not self.backend.base_url:
            return self.backend
        url = routed_url(self.backend.base_url, granted_url)
        if url == self.backend.base_url:
            return self.backend
        if url not in self._routed:
            log.info("llm.routed", backend=self.backend.name, model=self.backend.model, base_url=url)
            self._routed[url] = self.backend.at(url)
        return self._routed[url]

    def close(self) -> None:
        self.backend.unload()
        for backend in self._routed.values():
            backend.unload()
        self._routed.clear()


__all__ = [
    "BACKENDS",
    "CHARS_PER_TOKEN",
    "LLM_CALLS",
    "CallGuard",
    "Completion",
    "LLMBudgetExceeded",
    "LLMGpuUnavailable",
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
