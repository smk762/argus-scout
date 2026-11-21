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
- The files it names (heartbeat policy, standing approvals, tariff) are reloaded while scout runs, and a bad
  edit to one of those degrades to defaults (or skips the bad rule) and is reported, so a typo can't stop
  status updates. See :mod:`argus_scout.heartbeat` and :mod:`argus_scout.approvals`.
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
from pydantic import (
    AfterValidator,
    BaseModel,
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
    fields: dict[str, str] = Field(default_factory=dict)
    """StatusEvent field -> dotted path within an item, e.g. ``{summary: labels.alertname}``."""
    auth_header: str = "Authorization"
    auth_env: EnvName | None = None
    """Env var holding the value sent in ``auth_header`` (for example ``Bearer abc...``)."""

    @field_validator("fields")
    @classmethod
    def _known_fields(cls, value: dict[str, str]) -> dict[str, str]:
        unknown = sorted(set(value) - EVENT_FIELDS)
        if unknown:
            raise ValueError(
                f"not status event fields: {', '.join(unknown)} (known: {', '.join(sorted(EVENT_FIELDS))})"
            )
        return value


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
    return kind if kind in BUILTIN_SOURCE_KINDS else "plugin"


SourceConfig = Annotated[
    Annotated[WebhookSource, Tag("webhook")]
    | Annotated[JsonPollSource, Tag("json_poll")]
    | Annotated[PluginSource, Tag("plugin")],
    Discriminator(_source_tag),
]
"""A ``sources:`` entry. Built-in kinds are validated here; any other kind by its plugin's model."""


class Briefing(_Section):
    """A scheduled report, e.g. ``{kind: morning, at: "07:30"}`` in the schedule's timezone."""

    kind: Name
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
    briefings: list[Briefing] = Field(default_factory=list)

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
    backend: Name
    """Backend name as argus-cortex knows it (e.g. ``ollama``, ``openai``)."""
    base_url: HttpUrl | None = None
    model: Name
    api_key_env: EnvName | None = None


class ServicesConfig(_Section):
    """Services scout talks to. All optional; a missing one turns off what depends on it."""

    prometheus_url: HttpUrl | None = None
    llm: LLMConfig | None = None
    echo_url: HttpUrl | None = None


class ServerConfig(_Section):
    host: str = "127.0.0.1"
    port: int = Field(default=8400, ge=1, le=65535)
    read_only: bool = True
    """Refuse every state-changing route. Turn off only once a token is set."""
    allowed_origins: list[str] = Field(default_factory=list)
    token_env: EnvName | None = "SCOUT_API_TOKEN"
    """Env var holding the bearer token for write routes."""


class ScoutConfig(_Section):
    """The whole of ``scout.yaml``."""

    files: FilesConfig = FilesConfig()
    # A dict default is validated like file input, so the default store path resolves beside the config too.
    store: StoreConfig = Field(default={}, validate_default=True)
    sources: list[SourceConfig] = Field(default_factory=list)
    schedule: ScheduleConfig = ScheduleConfig()
    services: ServicesConfig = ServicesConfig()
    server: ServerConfig = ServerConfig()

    _base_dir: Path | None = PrivateAttr(default=None)

    @property
    def base_dir(self) -> Path | None:
        """The directory relative paths resolved against (the config file's), when there was one.

        Plugin source configs are validated after loading, so they need it to resolve their paths."""
        return self._base_dir

    @model_validator(mode="before")
    @classmethod
    def _empty_sections(cls, data: Any) -> Any:
        # A section whose keys are all commented out parses as null: treat it as empty.
        if isinstance(data, dict):
            data = {k: ([] if k == "sources" else {}) if v is None else v for k, v in data.items()}
        return data

    @model_validator(mode="after")
    def _unique_source_names(self) -> ScoutConfig:
        seen: set[str] = set()
        for source in self.sources:
            if source.name in seen:
                raise ValueError(f"duplicate source name {source.name!r}")
            seen.add(source.name)
        return self

    def secret_env_names(self) -> list[str]:
        """Every environment variable this config names as holding a secret, in file order."""
        names = [self.server.token_env]
        names += [getattr(s, "token_env", None) or getattr(s, "auth_env", None) for s in self.sources]
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
