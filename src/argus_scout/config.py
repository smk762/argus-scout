"""The one config file: every other file scout reads, every service URL, and the schedule.

``scout.yaml`` is validated by :class:`ScoutConfig` (pydantic v2, unknown keys rejected at every level).
Relative paths in it resolve against the config file's own directory, so a config directory can be moved
or mounted anywhere.

**Secrets never live in the YAML.** The file names an environment variable (``token_env: SCOUT_API_TOKEN``)
and :func:`resolve_secret` reads it from the process environment. :func:`load_config` first loads a
``.env`` file beside the config into the environment, without overriding variables that are already set,
so a real environment always wins over the file. Resolved values come back as :class:`~pydantic.SecretStr`
and the config model itself holds only variable *names*, so neither its repr nor its dumps can leak one.

**Strict here, forgiving there.** The two kinds of config fail differently, on purpose:

- This top-level file is read once at startup, and :func:`load_config` raises :class:`ConfigError` on
  anything wrong with it (missing file, bad YAML, unknown key, bad value). Scout refuses to start on a
  broken config rather than guess at what you meant.
- The files it names (heartbeat policy, standing approvals, tariff, signal rules) are reloaded while scout
  runs, and a bad edit to one of those degrades to defaults (or skips the bad rule) and is reported, so a
  typo can't stop status updates. See :mod:`argus_scout.heartbeat`, :mod:`argus_scout.approvals` and
  :mod:`argus_scout.signals`.
"""

from __future__ import annotations

import os
import re
from collections.abc import MutableMapping
from datetime import time
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from argus_cortex.fs import PathRefused, safe_join
from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Discriminator,
    Field,
    PrivateAttr,
    SecretStr,
    Tag,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)

ENV_FILE = ".env"
CONFIG_FILE = "scout.yaml"

# StatusEvent fields a JSON poller may map response fields onto.
EVENT_FIELDS = frozenset(
    {
        "event_id",
        "source",
        "severity",
        "generated_at",
        "summary",
        "body_md",
        "speakable",
        "requires_ack",
        "quiet_exempt",
        "expires_at",
        "link",
        "payload",
    }
)

_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class ConfigError(Exception):
    """The config file is missing, unreadable or invalid. Scout should not start."""


# --- field types -------------------------------------------------------------------------------------------


def _resolve_path(value: Path, info: ValidationInfo) -> Path:
    value = value.expanduser()
    base = (info.context or {}).get("base_dir")
    if base is not None and not value.is_absolute():
        value = Path(base) / value
    return value


def _check_env_name(value: str) -> str:
    if not _ENV_NAME.match(value):
        raise ValueError(f"{value!r} is not an environment variable name; put the secret in .env and name it here")
    return value


def _check_url(value: str) -> str:
    """An http(s) URL with a host and, if given, a valid port. Checked here so a typo (``http://echo:80x``,
    ``http://prom:99999``) fails ``check config`` instead of the server's startup."""
    try:
        parts = urlsplit(value)
        host, _port = parts.hostname, parts.port  # .port raises ValueError for a non-numeric or out-of-range port
    except ValueError as exc:
        raise ValueError(f"{value!r} is not a valid http(s) URL: {exc}") from None
    if parts.scheme not in ("http", "https") or not host:
        raise ValueError(f"{value!r} is not an http(s) URL")
    return value.rstrip("/")


def _check_name(value: str) -> str:
    if not value.strip():
        raise ValueError("must not be empty")
    return value


def _null_as(empty: type) -> BeforeValidator:
    def coerce(value: Any) -> Any:
        return empty() if value is None else value

    return BeforeValidator(coerce)


NullAsEmptyList = _null_as(list)
"""Annotate a list field with this so a key whose entries are all commented out (YAML null) reads as ``[]``."""
NullAsEmptyDict = _null_as(dict)
"""The same for a mapping or a section: a null value reads as ``{}`` (a section's defaults)."""

ConfigPath = Annotated[Path, AfterValidator(_resolve_path)]
"""A path; relative ones resolve against the config file's directory."""
EnvName = Annotated[str, AfterValidator(_check_env_name)]
"""The *name* of an environment variable holding a secret, never the secret itself."""
HttpUrl = Annotated[str, AfterValidator(_check_url)]
"""An http(s) URL, kept as text (no normalisation beyond dropping a trailing slash)."""
Name = Annotated[str, AfterValidator(_check_name)]


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# --- sections ----------------------------------------------------------------------------------------------


class FilesConfig(_Section):
    """The policy files scout reads. Each is optional: a missing one means built-in defaults."""

    heartbeat: ConfigPath | None = None
    """The heartbeat policy (YAML): what is worth interrupting for, quiet and rest hours, per-source routing.
    Unset, or a bad file: the built-in defaults. See ``examples/heartbeat.yaml``."""
    approvals: ConfigPath | None = None
    """Standing approvals (YAML): rules that move matching proposals to ``approved``, never further. Unset: none.
    See ``examples/approvals.yaml``."""
    tariff: ConfigPath | None = None
    """A time-of-use energy tariff (YAML), for the narrative, ``/metrics`` and the tariff-aware signal rules.
    Unset: no tariff. See ``examples/tariff.yaml``."""
    signals: ConfigPath | None = None
    """Signal rules (YAML): PromQL questions that raise recommendations. Unset: the analysis cycle does nothing.
    See ``examples/signals.yaml``."""
    persona: ConfigPath | None = None
    """The narrator's persona (YAML). Unset, or a bad file: the neutral built-in persona."""
    memory: ConfigPath | None = None
    """A directory of operator-written markdown notes folded into briefings. Scout reads them and writes only
    its own recent-rejections file there (see :mod:`argus_scout.memory`)."""


class StoreConfig(_Section):
    """Where scout keeps events, recommendations, approvals and reports."""

    path: ConfigPath = Field(default=Path("scout.db"), validate_default=True)
    """The SQLite database file, created (with its directory) on first use and migrated on open. ``:memory:``
    keeps nothing across restarts."""


class SourceSettings(_Section):
    """The keys every source has. A source plugin's ``config_model`` subclasses this (see
    :mod:`argus_scout.sources`), narrowing ``kind`` to its own name and adding its own keys."""

    kind: Name
    """``webhook``, ``json_poll``, or a kind an installed plugin provides."""
    name: Name
    """Unique among sources. It is the ``source`` of every event this source produces, and the
    ``{source}`` in ``POST /api/events/{source}``."""


class WebhookSource(SourceSettings):
    """Producers POST status events to scout."""

    kind: Literal["webhook"]
    token_env: EnvName | None = None
    """Env var holding the bearer token this source must send. Unset means the server-wide token applies."""


class JsonPollSource(SourceSettings):
    """Scout GETs a JSON endpoint on an interval and maps its fields onto status events."""

    kind: Literal["json_poll"]
    url: HttpUrl
    """The status endpoint to GET. Only this host and port are allowed (a LAN address is fine); a redirect to
    another host must pass the public-address rule."""
    interval: float = Field(default=60.0, gt=0, description="Seconds between polls.")
    items: str | None = None
    """Dotted path to the list of items in the response (``data.alerts``). Unset: the response is one item."""
    fields: Annotated[dict[str, str], NullAsEmptyDict] = Field(default_factory=dict)
    """StatusEvent field -> dotted path within an item, e.g. ``{summary: labels.alertname}``. ``source`` can't be
    mapped: a polled source's events are always its own."""
    id: str | list[str] | None = None
    """Dotted path (or a list of them) to what identifies an item, e.g. ``[labels.alertname, labels.instance]``. The
    event id is derived from those values, so set it whenever two items can share a summary. Unset: see
    :mod:`argus_scout.sources.json_poll` for the default rule."""
    resolve_missing: bool = True
    """Close (``resolved``) this source's open events that a successful poll no longer lists. Turn it off for an
    endpoint that reports only changes rather than the full current state."""
    auth_header: str = "Authorization"
    """The header ``auth_env``'s value is sent in."""
    auth_env: EnvName | None = None
    """Env var holding the value sent in ``auth_header`` (for example ``Bearer abc...``)."""

    @field_validator("fields")
    @classmethod
    def _known_fields(cls, value: dict[str, str]) -> dict[str, str]:
        mappable = EVENT_FIELDS - {"source"}
        if "source" in value:
            raise ValueError("source can't be mapped: a polled source's events are always its own (its name)")
        unknown = sorted(set(value) - mappable)
        if unknown:
            raise ValueError(f"not status event fields: {', '.join(unknown)} (known: {', '.join(sorted(mappable))})")
        return value

    @field_validator("id")
    @classmethod
    def _id_paths(cls, value: str | list[str] | None) -> str | list[str] | None:
        paths = [value] if isinstance(value, str) else value or []
        if value is not None and (not paths or any(not p.strip() for p in paths)):
            raise ValueError("must be a dotted path or a non-empty list of them")
        return value

    @model_validator(mode="after")
    def _one_identity(self) -> JsonPollSource:
        if self.id is not None and "event_id" in self.fields:
            raise ValueError("set id or fields.event_id, not both")
        return self


class PluginSource(BaseModel):
    """A source of a kind that a plugin provides (anything but ``webhook`` and ``json_poll``).

    The config schema can't know a third-party kind's keys, so here only ``kind`` and ``name`` are
    checked and every other key is kept. :func:`argus_scout.sources.build_sources` then validates
    the whole entry against the plugin's own ``config_model``. An unknown kind or a bad key fails
    that one source (and ``argus-scout check config``), never the others.
    """

    model_config = ConfigDict(extra="allow", frozen=True)

    kind: Name
    name: Name


BUILTIN_SOURCE_KINDS = frozenset({"webhook", "json_poll"})


def _source_tag(value: Any) -> str:
    kind = value.get("kind") if isinstance(value, dict) else getattr(value, "kind", None)
    # A list or mapping kind is unhashable: send it to the plugin model, which reports ``kind`` as not text.
    return kind if isinstance(kind, str) and kind in BUILTIN_SOURCE_KINDS else "plugin"


SourceConfig = Annotated[
    Annotated[WebhookSource, Tag("webhook")]
    | Annotated[JsonPollSource, Tag("json_poll")]
    | Annotated[PluginSource, Tag("plugin")],
    Discriminator(_source_tag),
]
"""A ``sources:`` entry. Built-in kinds are validated here; any other kind by its plugin's model."""


# --- delivery channels -------------------------------------------------------------------------------------


def _check_channel_file(value: str) -> str:
    # The file is fenced under the channel's dir again at write time (symlinks followed); this catches the
    # obvious mistakes at load, so `check config` reports them.
    try:
        rendered = value.format(kind="kind", date="date")
    except (KeyError, IndexError, ValueError) as exc:
        raise ValueError(f"file: only {{kind}} and {{date}} may be filled in ({exc})") from exc
    try:
        safe_join("/nonexistent-root", rendered, label="file")
    except PathRefused as exc:
        raise ValueError(str(exc)) from exc
    return value


class ChannelSettings(_Section):
    """The keys every channel has. A channel plugin's ``config_model`` subclasses this (see
    :mod:`argus_scout.channels`), narrowing ``kind`` to its own name and adding its own keys."""

    kind: Name
    """``text``, ``webhook``, ``snapcast``, ``mask``, or a kind an installed plugin provides."""
    name: Name
    """Unique among channels. Delivery records are keyed by it, so renaming a channel makes it a new one."""
    enabled: bool = True
    """``false`` keeps the entry but sends nothing."""
    min_severity: Literal["info", "notice", "warning", "critical"] = "info"
    """Deliver only messages at or above this severity. A report's severity is that of the worst item in it (a
    briefing also counts its open recommendations); a report with nothing open is ``info``."""
    kinds: Annotated[list[str], NullAsEmptyList] = Field(default_factory=list)
    """Deliver only these kinds (report kinds such as ``morning``, or ``event`` for an inbox item). Empty: all."""
    timeout: float = Field(default=30.0, gt=0, le=600, description="Seconds one delivery may take, retries included.")


class TextChannel(ChannelSettings):
    """Append each message to a file under ``dir``. The file is fenced under ``dir``: no ``..``, no absolute
    path, no symlink out."""

    kind: Literal["text"]
    dir: ConfigPath = Field(default=Path("."), validate_default=True)
    """The root everything is written under. Relative to the config's directory."""
    file: Annotated[str, AfterValidator(_check_channel_file)] = "scout-reports.log"
    """Relative to ``dir``. ``{kind}`` and ``{date}`` (YYYY-MM-DD, UTC) are filled in per message."""
    rotate_bytes: int | None = Field(default=None, gt=0)
    """Rotate the file (``x.log`` -> ``x.log.1`` ...) before it would grow past this size. Unset: never."""
    keep: int = Field(default=3, ge=1, le=100)
    """Rotated files to keep."""


class WebhookChannel(ChannelSettings):
    """POST each message as JSON to ``url`` (only that host is allowed)."""

    kind: Literal["webhook"]
    url: HttpUrl
    """Where to POST. Only this host and port are contacted (a LAN receiver is fine); redirects are never followed."""
    token_env: EnvName | None = None
    """Env var holding a bearer token to send. Unset: no Authorization header."""
    secret_env: EnvName | None = None
    """Env var holding an HMAC-SHA256 key; set, every POST carries an ``X-Scout-Signature`` header."""
    retries: int = Field(default=2, ge=0, le=5)
    """Retries after a failed attempt (connection error, 408, 429 or 5xx). Any other 4xx is final and permanent:
    the redelivery sweep won't retry it either."""
    backoff: float = Field(default=1.0, ge=0, le=60)
    """Seconds before the first retry; doubles each time, up to 60."""


class PluginChannel(BaseModel):
    """A channel of a kind that a plugin provides (anything but ``text`` and ``webhook``).

    Only ``kind`` and ``name`` are checked here; :func:`argus_scout.channels.build_channels` validates the
    whole entry against the plugin's own ``config_model``. A bad entry fails that one channel, never the others.
    """

    model_config = ConfigDict(extra="allow", frozen=True)

    kind: Name
    name: Name


BUILTIN_CHANNEL_KINDS = frozenset({"text", "webhook"})


def _channel_tag(value: Any) -> str:
    kind = value.get("kind") if isinstance(value, dict) else getattr(value, "kind", None)
    # A list or mapping kind is unhashable: send it to the plugin model, which reports ``kind`` as not text.
    return kind if isinstance(kind, str) and kind in BUILTIN_CHANNEL_KINDS else "plugin"


ChannelConfig = Annotated[
    Annotated[TextChannel, Tag("text")]
    | Annotated[WebhookChannel, Tag("webhook")]
    | Annotated[PluginChannel, Tag("plugin")],
    Discriminator(_channel_tag),
]
"""A ``channels:`` entry. Built-in kinds are validated here; any other kind by its plugin's model."""


class Briefing(_Section):
    """A scheduled report, e.g. ``{kind: morning, at: "07:30"}`` in the schedule's timezone."""

    kind: Literal["morning", "evening", "status"]
    """The report to write."""
    at: str
    """24-hour ``HH:MM`` in ``schedule.timezone`` (quote it in YAML: ``"07:30"``)."""

    @field_validator("at")
    @classmethod
    def _hhmm(cls, value: str) -> str:
        if not _HHMM.match(value):
            raise ValueError(f"{value!r} is not a 24-hour HH:MM time")
        return value

    @property
    def at_time(self) -> time:
        hour, minute = self.at.split(":")
        return time(int(hour), int(minute))


class ScheduleConfig(_Section):
    """When scout's jobs run. A restart neither repeats a briefing that already went out nor resets an interval."""

    analysis_interval: float = Field(default=900.0, gt=0, description="Seconds between analysis cycles.")
    status_interval: float = Field(default=300.0, gt=0, description="Seconds between status cycles.")
    timezone: str = "UTC"
    """An IANA zone name (``Europe/London``). Briefing times, quiet and rest hours and tariff periods are read
    in it."""
    briefings: Annotated[list[Briefing], NullAsEmptyList] = Field(default_factory=list)
    """Scheduled reports, one job each. A kind may appear at several times, never twice at the same time. A slot
    missed while scout was down runs late only if it passed less than 30 minutes ago."""

    @field_validator("briefings")
    @classmethod
    def _one_briefing_per_slot(cls, value: list[Briefing]) -> list[Briefing]:
        seen: set[tuple[str, str]] = set()
        for briefing in value:
            if (briefing.kind, briefing.at) in seen:
                raise ValueError(f"duplicate briefing: {briefing.kind} at {briefing.at}")
            seen.add((briefing.kind, briefing.at))
        return value

    @field_validator("timezone")
    @classmethod
    def _known_zone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown timezone {value!r}") from exc
        return value

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


GpuPriority = Literal["realtime", "interactive", "batch", "background"]
"""argus-cortex's ``PriorityClass``, highest first (kept literal here so config never imports the GPU client)."""

TARGET_RE = re.compile(r"^[a-z][a-z0-9-]*(:\S+)?$")


def _check_target(value: str) -> str:
    if not TARGET_RE.match(value) or len(value) > 200:
        raise ValueError("must be <service> or <service>:<model>, e.g. ollama or vllm:qwen3-8b")
    return value


class LLMGpuConfig(_Section):
    """A GPU lease around each model call (argus-iris broker, through ``argus_cortex.gpu``). See :mod:`argus_scout.gpu`.

    Present (even empty, ``gpu: {}``) means every narrative model call holds a lease for its HTTP round-trip;
    absent means no lease, which is right for a hosted backend. Which coordinator grants it (none, a host lock,
    the broker) is the host-wide ``ARGUS_GPU_*`` environment, never this file. The defaults are an 8B model in
    Ollama and its KV cache, the figures ``gpu-tenant.yaml`` declares.
    """

    min_vram_mb: int = Field(default=6000, gt=0, description="The whole footprint: weights plus working set, MiB.")
    model_mb: int | None = Field(
        default=5200, ge=0, description="The part of min_vram_mb that stays loaded in the model server after a call."
    )
    priority: GpuPriority = Field(default="background", description="Briefings can wait: the lowest class.")
    target: Annotated[str, AfterValidator(_check_target)] | None = Field(
        default=None,
        description="The broker service that serves the model, `<service>` or `<service>:<model>`. Default for ollama: "
        "`ollama:<model>`. Required for openai.",
    )

    @model_validator(mode="after")
    def _model_within_total(self) -> LLMGpuConfig:
        if self.model_mb is not None and self.model_mb > self.min_vram_mb:
            raise ValueError(f"model_mb ({self.model_mb}) cannot exceed min_vram_mb ({self.min_vram_mb})")
        return self


class LLMConfig(_Section):
    """The model the narrative is written with. See :mod:`argus_scout.llm`.

    ``ollama`` talks to Ollama's ``/api/chat``. ``openai`` talks to any OpenAI-compatible
    ``/chat/completions`` (a local server, or a hosted provider with ``api_key_env``); its ``base_url``
    includes the API prefix, e.g. ``http://llm:8000/v1``.
    """

    backend: Literal["ollama", "openai"]
    """``ollama`` (Ollama's ``/api/chat``) or ``openai`` (any OpenAI-compatible ``/chat/completions``)."""
    base_url: HttpUrl
    """The model server. For ``openai``, include the API prefix: ``http://llm:8000/v1``."""
    model: Name
    """The model name the server knows it by."""
    api_key_env: EnvName | None = None
    """Env var holding an API key, sent as a bearer token (hosted backends). Unset: no key."""
    timeout: float = Field(default=120.0, gt=0, description="Seconds before one model call gives up.")
    max_tokens: int = Field(default=400, gt=0, description="Most tokens one report may generate.")
    context_tokens: int = Field(
        default=3072, ge=1024, description="The model's context window; prompts are cut to fit it with the output."
    )
    temperature: float | None = Field(default=None, ge=0, le=2)
    """Sampling temperature. Unset: the server's default."""
    gpu: LLMGpuConfig | None = None
    """Hold a GPU lease around each model call. Leave unset for a hosted backend."""

    @model_validator(mode="after")
    def _gpu_target_for_openai(self) -> LLMConfig:
        # The broker can only place a lease on a service its operator configured. For Ollama that is
        # ollama:<model>; an OpenAI-compatible server has no implied service, and a hosted one has none at all.
        if self.gpu is not None and self.backend == "openai" and self.gpu.target is None:
            raise ValueError(
                "gpu with backend openai needs gpu.target (the broker service serving the model); "
                "a hosted backend takes no GPU lease, so leave gpu unset"
            )
        return self


ECHO_REQUEST_FIELDS = frozenset(
    {"text", "voice_clone_id", "speaker", "language", "voice_description", "instruction", "emotion", "speed"}
)
"""argus-echo's shared ``SynthesiseRequest`` fields (``argus_echo.contract``)."""


class EchoConfig(_Section):
    """argus-echo, the speech server voice channels play from. See :mod:`argus_scout.channels.echo`.

    The voice keys are argus-echo's shared request fields, sent as they are: ``voice_clone_id`` (a stored clone;
    wins over ``speaker``), ``speaker`` (a built-in speaker), ``language``, and the Qwen3-TTS-only
    ``instruction``, ``voice_description`` and ``emotion`` (other engines ignore them). ``extra`` passes
    engine-specific fields through (Miso's ``temperature``, ``topk``). Scout never holds a GPU lease around a
    synthesis: echo takes its own.
    """

    url: HttpUrl
    """The engine's root, e.g. ``http://echo:8000``: scout calls ``/health`` and ``/synthesise/stream`` under it."""
    voice_clone_id: str | None = None
    """A clone stored on that engine (clones are per engine). Wins over ``speaker``. Speech in a clone is marked
    as cloned: the ``mask`` channel discloses it over ``transport: websocket`` when ``cloned_voice_consented`` attests consent, and over http refuses it unless its
    ``allow_cloned_voice`` is set."""
    speaker: str | None = None
    """A built-in speaker (``GET /speakers`` on the engine). Neither this nor a clone: the engine's default voice."""
    language: str = "en"
    """ISO language code. Miso speaks English only."""
    instruction: str | None = None
    """Qwen3-TTS only: a speaking instruction. Other engines ignore it."""
    voice_description: str | None = None
    """Qwen3-TTS only: a designed voice, described in words. Other engines ignore it."""
    emotion: str | None = None
    """Qwen3-TTS only. Other engines ignore it."""
    extra: Annotated[dict[str, Any], NullAsEmptyDict] = Field(default_factory=dict)
    """Engine-specific request fields, passed through (Miso: ``temperature``, ``topk``)."""
    format: Literal["pcm_s16le", "wav"] = "pcm_s16le"
    """The audio handed to voice channels: raw 16-bit little-endian PCM, or the same in a WAV container."""
    timeout: float = Field(default=60.0, gt=0, le=600, description="Seconds one synthesis may take, model warm.")
    load_timeout: float = Field(
        default=180.0, gt=0, le=1800, description="Seconds one synthesis may take when the model has to load first."
    )
    health_timeout: float = Field(default=5.0, gt=0, le=60, description="Seconds the readiness check may take.")
    max_seconds: float = Field(default=300.0, gt=0, le=3600, description="The longest speech accepted, in seconds.")

    @field_validator("extra")
    @classmethod
    def _no_shadowing(cls, value: dict[str, Any]) -> dict[str, Any]:
        shadowed = sorted(set(value) & ECHO_REQUEST_FIELDS)
        if shadowed:
            raise ValueError(f"set {', '.join(shadowed)} as its own key, not under extra")
        return value


class ServicesConfig(_Section):
    """Services scout talks to. All optional; a missing one turns off what depends on it."""

    prometheus_url: HttpUrl | None = None
    """Prometheus's root (``http://prometheus:9090``), queried read-only over the PromQL HTTP API. Scout never
    scrapes or connects to the hosts it reports on. Unset: no signal rules run."""
    llm: LLMConfig | None = None
    """The model reports are written with. Unset: every report comes from the plain template."""
    echo: EchoConfig | None = None
    """argus-echo for speech. Unset: nothing is synthesised, so voice channels stay silent; text still goes out."""

    @model_validator(mode="before")
    @classmethod
    def _moved_keys(cls, data: Any) -> Any:
        if isinstance(data, dict) and "echo_url" in data:
            raise ValueError("echo_url was replaced by echo: {url: ...} (see docs/channels.md)")
        return data


class DeliveryConfig(_Section):
    """The redelivery sweep (:mod:`argus_scout.channels.redeliver`): a report a channel failed to take is tried
    again on an interval, until it succeeds, the channel refuses it permanently, or the report is too old."""

    retry_interval: float = Field(default=300.0, gt=0, description="Seconds between redelivery sweeps.")
    retry_max_age: float = Field(
        default=6 * 3600.0, gt=0, description="Seconds after a report was written that it is still retried."
    )
    max_attempts: int = Field(default=5, ge=1, le=100, description="Failed attempts per channel before giving up.")
    speak_max_age: float = Field(
        default=600.0, ge=0, description="Seconds after a report was written that a retry may still speak it."
    )
    """Older than this, a retry goes to text channels only: late speech is worse than none. ``0``: never re-speak."""


class BudgetConfig(_Section):
    """Daily usage caps (see :mod:`argus_scout.budget`). ``limits`` maps a kind to calls per day; a missing kind
    or ``0`` is unlimited. The narrative charges each model call to ``llm_calls``."""

    limits: Annotated[dict[Name, Annotated[int, Field(ge=0)]], NullAsEmptyDict] = Field(default_factory=dict)
    """Kind -> calls per day. Scout charges ``llm_calls`` (each report's model call), ``tts`` (each speech
    synthesis) and ``speak`` (each spoken message). Counts live in the store and reset at midnight in
    ``schedule.timezone``."""
    block_manual: bool = False
    """Refuse over-cap work done while serving an HTTP request too. Off: such work goes through and is logged.
    Scheduled jobs and the CLI are always held to the caps."""


class ServerConfig(_Section):
    """The HTTP API that ``argus-scout serve`` runs. The defaults are closed: loopback only, read-only, and with
    no token set every write is refused."""

    host: str = "127.0.0.1"
    """Bind address (``serve --host`` overrides it). Reads are open unless ``read_auth``: keep it on loopback."""
    port: int = Field(default=8400, ge=1, le=65535)
    """TCP port (``serve --port`` overrides it)."""
    read_only: bool = True
    """Refuse every state-changing route (webhook pushes included). Turn off only once a token is set."""
    allowed_origins: Annotated[list[str], NullAsEmptyList] = Field(default_factory=list)
    """Browser origins trusted to make writes (``https://ui.example``). No ``Origin`` header (curl, scripts) and
    a same-host IP or ``localhost`` origin are always allowed; any other cross-origin write is refused."""
    token_env: EnvName | None = "SCOUT_API_TOKEN"
    """Env var holding the bearer token for write routes. Unset or empty means every write route is closed."""
    read_auth: bool = False
    """Require the same bearer token on the read API (``/api/*`` GETs and ``/wall``). Off by default: scout binds
    to loopback, where reaching the port is already host access. Turn it on before binding anywhere else.
    ``/health`` and ``/metrics`` stay open either way."""


class ScoutConfig(_Section):
    """The whole of ``scout.yaml``."""

    # A section whose keys are all commented out parses as null: NullAsEmpty* reads it as empty (defaults).
    files: Annotated[FilesConfig, NullAsEmptyDict] = FilesConfig()
    """The policy files scout reads (reloaded while it runs)."""
    # A dict default is validated like file input, so the default store path resolves beside the config too.
    store: Annotated[StoreConfig, NullAsEmptyDict] = Field(default={}, validate_default=True)
    """Where scout keeps its state."""
    sources: Annotated[list[SourceConfig], NullAsEmptyList] = Field(default_factory=list)
    """Where status events come from."""
    channels: Annotated[list[ChannelConfig], NullAsEmptyList] = Field(default_factory=list)
    """Where reports go."""
    schedule: Annotated[ScheduleConfig, NullAsEmptyDict] = ScheduleConfig()
    """Cycle intervals, the timezone and the briefings."""
    services: Annotated[ServicesConfig, NullAsEmptyDict] = ServicesConfig()
    """Prometheus, the model and argus-echo."""
    budget: Annotated[BudgetConfig, NullAsEmptyDict] = BudgetConfig()
    """Daily usage caps."""
    delivery: Annotated[DeliveryConfig, NullAsEmptyDict] = DeliveryConfig()
    """The redelivery sweep."""
    server: Annotated[ServerConfig, NullAsEmptyDict] = ServerConfig()
    """The HTTP API."""

    _base_dir: Path | None = PrivateAttr(default=None)

    @property
    def base_dir(self) -> Path | None:
        """The directory relative paths resolved against (the config file's), when there was one.

        Plugin source and channel configs are validated after loading, so they need it to resolve their paths."""
        return self._base_dir

    @model_validator(mode="after")
    def _unique_source_names(self) -> ScoutConfig:
        seen: set[str] = set()
        for source in self.sources:
            if source.name in seen:
                raise ValueError(f"duplicate source name {source.name!r}")
            seen.add(source.name)
        return self

    @model_validator(mode="after")
    def _unique_channel_names(self) -> ScoutConfig:
        seen: set[str] = set()
        for channel in self.channels:
            if channel.name in seen:
                raise ValueError(f"duplicate channel name {channel.name!r}")
            seen.add(channel.name)
        return self

    def secret_env_names(self) -> list[str]:
        """Every environment variable this config names as holding a secret, in file order."""
        names = [self.server.token_env]
        names += [getattr(s, "token_env", None) or getattr(s, "auth_env", None) for s in self.sources]
        for channel in self.channels:
            names += [getattr(channel, "token_env", None), getattr(channel, "secret_env", None)]
        if self.services.llm:
            names.append(self.services.llm.api_key_env)
        return list(dict.fromkeys(n for n in names if n))


# --- loading -----------------------------------------------------------------------------------------------


def parse_env_file(path: Path) -> dict[str, str]:
    """``KEY=value`` lines from a ``.env`` file: ``#`` comments, optional ``export``, optional quotes."""
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        line = line.removeprefix("export ").lstrip()
        key, sep, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not sep or not _ENV_NAME.match(key):
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].rstrip()
        values[key] = value
    return values


def load_env_file(path: Path, environ: MutableMapping[str, str] | None = None) -> list[str]:
    """Load a ``.env`` file into the environment without overriding what is already set.

    Returns the names it set. A missing file is not an error.
    """
    environ = os.environ if environ is None else environ
    if not path.is_file():
        return []
    loaded = []
    for key, value in parse_env_file(path).items():
        if key not in environ:
            environ[key] = value
            loaded.append(key)
    return loaded


def resolve_secret(env_name: str | None, environ: MutableMapping[str, str] | None = None) -> SecretStr | None:
    """The secret held in ``env_name``, or None when unnamed, unset or empty."""
    if not env_name:
        return None
    value = (os.environ if environ is None else environ).get(env_name, "").strip()
    return SecretStr(value) if value else None


def _format_errors(exc: ValidationError) -> str:
    lines = []
    for err in exc.errors():
        loc = ".".join(str(part) for part in err["loc"]) or "(top level)"
        msg = err["msg"].removeprefix("Value error, ")
        if err["type"] == "extra_forbidden":
            msg = "unknown key"
        lines.append(f"  {loc}: {msg}")
    return "\n".join(lines)


def parse_config(data: Any, base_dir: Path | None = None, *, source: str = "config") -> ScoutConfig:
    """Validate already-parsed YAML. Relative paths resolve against ``base_dir`` when given."""
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ConfigError(f"{source}: expected a mapping at the top level, got {type(data).__name__}")
    try:
        config = ScoutConfig.model_validate(data, context={"base_dir": base_dir})
    except ValidationError as exc:
        count = exc.error_count()
        raise ConfigError(f"{source}: {count} error{'s' * (count != 1)}\n{_format_errors(exc)}") from exc
    config._base_dir = base_dir
    return config


def load_config(path: str | Path, *, load_env: bool = True) -> ScoutConfig:
    """Load and validate ``scout.yaml``; raise :class:`ConfigError` if anything is wrong.

    With ``load_env`` (the default), a ``.env`` beside the config is loaded into ``os.environ`` first,
    never overriding variables already set, so :func:`resolve_secret` and argus-cortex's bearer auth see it.
    """
    path = Path(path).expanduser()
    try:
        text = path.read_text()
    except OSError as exc:
        raise ConfigError(f"{path}: cannot read config ({exc.strerror or exc})") from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid YAML: {exc}") from exc
    base_dir = path.resolve().parent
    config = parse_config(data, base_dir, source=str(path))
    if load_env:
        load_env_file(base_dir / ENV_FILE)
    return config
