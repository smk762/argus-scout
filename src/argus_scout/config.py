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
    if not value.startswith(("http://", "https://")) or len(value) <= len("https://"):
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
    approvals: ConfigPath | None = None
    tariff: ConfigPath | None = None
    signals: ConfigPath | None = None
    persona: ConfigPath | None = None
    """The narrator's persona (YAML). Unset, or a bad file: the neutral built-in persona."""
    memory: ConfigPath | None = None
    """A directory of operator-written markdown notes folded into briefings. Scout reads them and writes only
    its own recent-rejections file there (see :mod:`argus_scout.memory`)."""


class StoreConfig(_Section):
    """Where scout keeps events, recommendations, approvals and reports."""

    path: ConfigPath = Field(default=Path("scout.db"), validate_default=True)


class SourceSettings(_Section):
    """The keys every source has. A source plugin's ``config_model`` subclasses this (see
    :mod:`argus_scout.sources`), narrowing ``kind`` to its own name and adding its own keys."""

    kind: Name
    name: Name


class WebhookSource(SourceSettings):
    """Producers POST status events to scout."""

    kind: Literal["webhook"]
    token_env: EnvName | None = None
    """Env var holding the bearer token this source must send. Unset means the server-wide token applies."""


class JsonPollSource(SourceSettings):
    """Scout GETs a JSON endpoint on an interval and maps its fields onto status events."""

    kind: Literal["json_poll"]
    url: HttpUrl
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
    name: Name
    enabled: bool = True
    min_severity: Literal["info", "notice", "warning", "critical"] = "info"
    """Deliver only messages at or above this severity."""
    kinds: Annotated[list[str], NullAsEmptyList] = Field(default_factory=list)
    """Deliver only these kinds (report kinds such as ``morning``, or ``event`` for an inbox item). Empty: all."""
    timeout: float = Field(default=30.0, gt=0, le=600, description="Seconds one delivery may take, retries included.")


class TextChannel(ChannelSettings):
    """Append each message to a file under ``dir``. The file is fenced under ``dir``: no ``..``, no absolute
    path, no symlink out."""

    kind: Literal["text"]
    dir: ConfigPath = Field(default=Path("."), validate_default=True)
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
    token_env: EnvName | None = None
    """Env var holding a bearer token to send. Unset: no Authorization header."""
    secret_env: EnvName | None = None
    """Env var holding an HMAC-SHA256 key; set, every POST carries an ``X-Scout-Signature`` header."""
    retries: int = Field(default=2, ge=0, le=5)
    """Retries after a failed attempt (connection error, 429 or 5xx); a 4xx is not retried."""
    backoff: float = Field(default=1.0, ge=0, le=60)
    """Seconds before the first retry; doubles each time."""


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
    at: str

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
    analysis_interval: float = Field(default=900.0, gt=0, description="Seconds between analysis cycles.")
    status_interval: float = Field(default=300.0, gt=0, description="Seconds between status cycles.")
    timezone: str = "UTC"
    briefings: Annotated[list[Briefing], NullAsEmptyList] = Field(default_factory=list)

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


class LLMConfig(_Section):
    """The model the narrative is written with. See :mod:`argus_scout.llm`.

    ``ollama`` talks to Ollama's ``/api/chat``. ``openai`` talks to any OpenAI-compatible
    ``/chat/completions`` (a local server, or a hosted provider with ``api_key_env``); its ``base_url``
    includes the API prefix, e.g. ``http://llm:8000/v1``.
    """

    backend: Literal["ollama", "openai"]
    base_url: HttpUrl
    model: Name
    api_key_env: EnvName | None = None
    timeout: float = Field(default=120.0, gt=0, description="Seconds before one model call gives up.")
    max_tokens: int = Field(default=400, gt=0, description="Most tokens one report may generate.")
    context_tokens: int = Field(
        default=3072, ge=1024, description="The model's context window; prompts are cut to fit it with the output."
    )
    temperature: float | None = Field(default=None, ge=0, le=2)


class ServicesConfig(_Section):
    """Services scout talks to. All optional; a missing one turns off what depends on it."""

    prometheus_url: HttpUrl | None = None
    llm: LLMConfig | None = None
    echo_url: HttpUrl | None = None


class BudgetConfig(_Section):
    """Daily usage caps (see :mod:`argus_scout.budget`). ``limits`` maps a kind to calls per day; a missing kind
    or ``0`` is unlimited. The narrative charges each model call to ``llm_calls``."""

    limits: Annotated[dict[Name, Annotated[int, Field(ge=0)]], NullAsEmptyDict] = Field(default_factory=dict)
    block_manual: bool = False
    """Refuse over-cap work started by a person too (HTTP requests, the CLI), not only scheduled work."""


class ServerConfig(_Section):
    host: str = "127.0.0.1"
    port: int = Field(default=8400, ge=1, le=65535)
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
    # A dict default is validated like file input, so the default store path resolves beside the config too.
    store: Annotated[StoreConfig, NullAsEmptyDict] = Field(default={}, validate_default=True)
    sources: Annotated[list[SourceConfig], NullAsEmptyList] = Field(default_factory=list)
    channels: Annotated[list[ChannelConfig], NullAsEmptyList] = Field(default_factory=list)
    schedule: Annotated[ScheduleConfig, NullAsEmptyDict] = ScheduleConfig()
    services: Annotated[ServicesConfig, NullAsEmptyDict] = ServicesConfig()
    budget: Annotated[BudgetConfig, NullAsEmptyDict] = BudgetConfig()
    server: Annotated[ServerConfig, NullAsEmptyDict] = ServerConfig()

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
