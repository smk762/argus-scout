"""GPU leases from the suite's broker (argus-iris), through argus-cortex's coordinator.

Scout runs no model on a GPU itself: a model server (Ollama, or an OpenAI-compatible one such as vLLM) does. When
``services.llm.gpu`` is set, each narrative model call holds one lease, of kind ``narrative``, for exactly its HTTP
round-trip (:class:`~argus_scout.llm.TextGenerator`'s call guard), never while facts are gathered, a prompt is built
or a report is stored. That lets the broker queue the call behind more urgent work, place it on a node with room and
evict an idle model first, instead of scout loading a model onto a card someone else is using.

* **The coordinator** is :func:`argus_cortex.gpu.coordinator_from_env`: the host-wide ``ARGUS_GPU_*`` names every argus
  process on the host shares, from the environment or the suite settings file (``ARGUS_GPU_BROKER_URL`` and
  ``ARGUS_GPU_BROKER_TOKEN`` for argus-iris, which is used whenever the URL is set; ``ARGUS_GPU_COORDINATOR=lease``
  for a host lock without one; ``ARGUS_GPU_LEASE_TIMEOUT_S``...). Nothing set is ``none``: no gating, every lease
  granted at once. There is deliberately no scout.yaml knob for it: a lock path or broker URL that differs from its
  siblings' would let scout skip the queue they all wait in. The broker falls back to ``ARGUS_GPU_BROKER_FALLBACK``
  (``lease`` by default) when iris cannot be reached.
* **The lease** asks for ``min_vram_mb`` (``model_mb`` of it weights the server keeps loaded) on the target
  ``ollama:<model>`` for the ollama backend, or ``services.llm.gpu.target`` for an OpenAI-compatible server. It is
  **preemptible** at priority ``background`` by default: a briefing can wait, and a yield request needs no action
  from scout, whose single call releases the lease as soon as it returns.
* **Where the call goes.** A grant's ``base_url`` is the model server on the node the broker chose, and scout calls
  it there (as argus-darkroom does for its Ollama expansion and ComfyUI renders): the broker reserved room on that
  node, not on whichever one scout's config names. Its scheme, host and port replace the configured URL's; when it
  has no path of its own the configured path (an OpenAI-compatible ``/v1``) is kept. ``None`` (no broker, a
  fallback grant) means the configured URL.
* **No lease in time** (:data:`LEASE_ERRORS`: the coordinator's timeout passed, the broker refused, or the broker
  answered with an error) is :class:`~argus_scout.llm.LLMGpuUnavailable`; the narrative writes the report from its
  template with ``fallback_reason: gpu_unavailable``. A briefing is never skipped for want of a GPU, and a call that
  never ran is not charged to the daily cap.

**Speech is not scout's GPU work.** Voice synthesis is argus-echo's: echo's engines are residents the broker reads,
evicts and wakes on echo's behalf, under echo's own manifest. Scout must not hold a lease around a request to echo:
it would claim VRAM for a model scout does not load, double-count echo's footprint and could block echo's own
placement. This is what replaces the private deployment's ad-hoc voice backoff (stay text-only for a while after a
synthesis failed, mostly because the narrative's model and the TTS model were fighting over one card): scout's model
call now queues through the broker and has released its lease before any delivery starts, and the broker makes room
for echo's model when echo needs it. A delivery that fails is the channel's to report; nothing here retries it.
:func:`narrative_guard` is the only place scout takes a lease (``tests/test_gpu.py`` checks it).
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager
from urllib.parse import urlsplit, urlunsplit

from argus_cortex.gpu import GpuCoordinator, GpuLease, GpuLeaseRefused, GpuLeaseTimeout, coordinator_from_env
from argus_cortex.gpu_broker import BrokerError

from argus_scout.config import LLMConfig, LLMGpuConfig

TENANT = "argus-scout"
"""The tenant name in ``gpu-tenant.yaml`` (and ``ARGUS_GPU_TENANT``, if a host sets it)."""

KIND = "narrative"
"""The one kind of GPU work scout leases: a report's model call."""

PREEMPTIBLE = True
"""Scout's call can always be asked to yield: it releases the lease the moment its one round-trip returns."""

DEFAULTS = LLMGpuConfig()
"""The footprint and priority when ``services.llm.gpu`` names none: the figures the manifest declares."""

LEASE_ERRORS: tuple[type[Exception], ...] = (GpuLeaseRefused, GpuLeaseTimeout, BrokerError)
"""A lease that was not granted: the report falls back to its template."""

CallGuard = Callable[[], AbstractAsyncContextManager[GpuLease]]


def default_coordinator() -> GpuCoordinator:
    """The coordinator the host-wide ``ARGUS_GPU_*`` environment names (``none`` when unset)."""
    return coordinator_from_env()


def lease_target(llm: LLMConfig) -> str:
    """The broker target for ``llm``'s model calls: ``gpu.target`` if set, else ``ollama:<model>``."""
    if llm.gpu is not None and llm.gpu.target:
        return llm.gpu.target
    return f"ollama:{llm.model.strip()}"


def routed_url(configured: str, granted: str | None) -> str:
    """The URL to call: the broker's ``granted`` base URL on the node it chose, or ``configured`` without one.

    A granted URL with no path keeps the configured path, so ``http://llm:8000/v1`` granted on
    ``http://node-b:8000`` becomes ``http://node-b:8000/v1``.
    """
    if not granted:
        return configured
    parts = urlsplit(granted.strip())
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise BrokerError(f"the GPU broker named an unusable model URL: {granted!r}")
    path = parts.path.rstrip("/") or urlsplit(configured).path.rstrip("/")
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def narrative_guard(llm: LLMConfig, coordinator: GpuCoordinator | None = None) -> CallGuard | None:
    """The call guard for :class:`~argus_scout.llm.TextGenerator`, or ``None`` when ``services.llm.gpu`` is unset.

    Each call of the guard takes one preemptible lease of kind :data:`KIND`. ``coordinator`` defaults to
    :func:`default_coordinator`, built once here so the broker client (and its registration) is reused.
    """
    gpu = llm.gpu
    if gpu is None:
        return None
    coord = coordinator if coordinator is not None else default_coordinator()
    target = lease_target(llm)

    @contextlib.asynccontextmanager
    async def guard() -> AsyncIterator[GpuLease]:
        async with coord.alease(
            caller=f"{TENANT}:{llm.model}",
            min_vram_mb=gpu.min_vram_mb,
            model_mb=gpu.model_mb,
            priority=gpu.priority,
            target=target,
            kind=KIND,
            preemptible=PREEMPTIBLE,
        ) as lease:
            yield lease

    return guard


__all__ = [
    "DEFAULTS",
    "KIND",
    "LEASE_ERRORS",
    "PREEMPTIBLE",
    "TENANT",
    "CallGuard",
    "default_coordinator",
    "lease_target",
    "narrative_guard",
    "routed_url",
]
