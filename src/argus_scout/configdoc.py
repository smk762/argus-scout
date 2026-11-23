"""Generate ``docs/config.md``, the ``scout.yaml`` reference, from the pydantic models in :mod:`argus_scout.config`.

Every key's type, default, constraints and description come from the models themselves: the type annotation,
the default, the ``Field`` constraints (``gt``, ``le``, ``pattern``...), and the ``description=`` or the docstring
written under the field. So the reference can't drift from the schema: ``make docs`` rewrites it, and
``tests/test_configdoc.py`` fails when the committed copy differs from :func:`render`.

Each section's example is validated against the model when the page is rendered, so a stale example fails the
build too. Run ``python -m argus_scout.configdoc [OUT]`` (``make docs`` does) to write it.
"""

from __future__ import annotations

import ast
import inspect
import re
import sys
import textwrap
import types
from dataclasses import dataclass
from enum import Enum
from functools import cache
from pathlib import Path
from typing import Annotated, Any, Literal, Union, get_args, get_origin

import annotated_types as at
import yaml
from pydantic import BaseModel
from pydantic.fields import FieldInfo
from pydantic_core import PydanticUndefined

from argus_scout import config as cfg
from argus_scout.channels.mask import MaskConfig
from argus_scout.channels.snapcast import SnapcastConfig

OUTPUT = Path("docs/config.md")

# --- reading the models --------------------------------------------------------------------------------------


@cache
def _own_fields(model: type) -> frozenset[str]:
    """The fields ``model``'s own class body declares (not those it only inherits)."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(model)))
    body = tree.body[0].body if tree.body and isinstance(tree.body[0], ast.ClassDef) else []
    return frozenset(n.target.id for n in body if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name))


@cache
def _attribute_docs(model: type) -> dict[str, str]:
    """``field -> docstring`` for the string literals written directly under fields, across the model's bases."""
    docs: dict[str, str] = {}
    for klass in reversed(model.__mro__):
        if not (isinstance(klass, type) and issubclass(klass, BaseModel)) or klass is BaseModel:
            continue
        try:
            tree = ast.parse(textwrap.dedent(inspect.getsource(klass)))
        except (OSError, TypeError):  # pragma: no cover - source not available
            continue
        body = tree.body[0].body if tree.body and isinstance(tree.body[0], ast.ClassDef) else []
        for node, after in zip(body, body[1:], strict=False):
            if (
                isinstance(node, ast.AnnAssign)
                and isinstance(node.target, ast.Name)
                and isinstance(after, ast.Expr)
                and isinstance(after.value, ast.Constant)
                and isinstance(after.value.value, str)
            ):
                docs[node.target.id] = inspect.cleandoc(after.value.value)
    return docs


_ROLE = re.compile(r":(?:class|func|mod|meth|data|attr):`(~?)([^`]+)`")


def prose(text: str | None) -> str:
    """reST-ish docstring text as one line of markdown."""
    if not text:
        return ""
    text = _ROLE.sub(lambda m: f"`{m.group(2).rsplit('.', 1)[-1] if m.group(1) else m.group(2)}`", text)
    text = text.replace("``", "`")
    return " ".join(text.split())


def describe(model: type[BaseModel], name: str, field: FieldInfo) -> str:
    parts = [prose(field.description), prose(_attribute_docs(model).get(name))]
    return " ".join(p for p in parts if p)


# Validators that give a plain type its meaning in scout.yaml.
_VALIDATED: dict[Any, str] = {
    cfg._resolve_path: "path",
    cfg._check_env_name: "env var name",
    cfg._check_url: "URL",
    cfg._check_name: "text",
    cfg._check_target: "text",
    cfg._check_channel_file: "text",
}
_PLAIN = {str: "text", int: "integer", float: "number", bool: "boolean", Path: "path", Any: "any"}


def _validated_name(metadata: list[Any]) -> str | None:
    for item in metadata:
        func = getattr(item, "func", None)
        if func in _VALIDATED:
            return _VALIDATED[func]
    return None


def type_name(tp: Any, links: dict[type, tuple[str, str]], metadata: list[Any] | None = None) -> str:
    named = _validated_name(metadata or [])
    if named:
        return named
    origin, args = get_origin(tp), get_args(tp)
    if origin is Annotated:
        return type_name(args[0], links, list(args[1:]))
    if origin in (Union, types.UnionType):
        members = [a for a in args if a is not type(None)]
        if len(members) > 1 and all(isinstance(a, type) or get_origin(a) is Annotated for a in members):
            # The sources/channels unions: built-in kinds plus plugins.
            kinds = [get_args(a)[0] if get_origin(a) is Annotated else a for a in members]
            if all(k in links for k in kinds):
                return " or ".join(type_name(k, links) for k in kinds)
        text = " or ".join(type_name(a, links) for a in members)
        return f"{text} or null" if type(None) in args else text
    if origin is Literal:
        return " \\| ".join(f"`{a}`" for a in args)
    if origin is list:
        return f"list of {type_name(args[0], links)}"
    if origin is dict:
        return f"map of {type_name(args[0], links)} to {type_name(args[1], links)}"
    if tp in links:
        return f"[{links[tp][0]}](#{links[tp][1]})"
    if isinstance(tp, type) and issubclass(tp, Enum):
        return " \\| ".join(f"`{member.value}`" for member in tp)
    return _PLAIN.get(tp, getattr(tp, "__name__", str(tp)))


def _num(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:g}"


def _constraint_parts(metadata: list[Any]) -> list[str]:
    parts = []
    for item in metadata:
        if isinstance(item, at.Gt):
            parts.append(f"> {_num(item.gt)}")
        elif isinstance(item, at.Ge):
            parts.append(f">= {_num(item.ge)}")
        elif isinstance(item, at.Lt):
            parts.append(f"< {_num(item.lt)}")
        elif isinstance(item, at.Le):
            parts.append(f"<= {_num(item.le)}")
        elif isinstance(item, at.MinLen):
            parts.append(f"at least {item.min_length} long")
        elif isinstance(item, at.MaxLen):
            parts.append(f"at most {item.max_length} long")
        elif getattr(item, "pattern", None):
            parts.append(f"matches `{item.pattern}`")
        elif isinstance(item, FieldInfo):
            parts.extend(_constraint_parts(item.metadata))
    return parts


def constraints(field: FieldInfo) -> str:
    parts = _constraint_parts(field.metadata)
    # Constraints on a dict's values (budget limits) live in the value's annotation.
    tp = field.annotation
    if get_origin(tp) is dict:
        value = get_args(tp)[1]
        if get_origin(value) is Annotated:
            inner = _constraint_parts(list(get_args(value)[1:]))
            parts.extend(f"each value {p}" for p in inner)
    return ", ".join(parts)


def default(field: FieldInfo) -> str:
    if field.default_factory is not None:
        value = field.default_factory()  # type: ignore[call-arg]
    elif field.default is PydanticUndefined:
        return "required"
    else:
        value = field.default
    if isinstance(value, BaseModel) or (isinstance(value, dict) and not value and field.validate_default):
        return "section defaults"
    if isinstance(value, Path):
        value = str(value)
    if isinstance(value, Enum):
        value = value.value
    if value is None:
        return "`null`"
    rendered = yaml.safe_dump(value, default_flow_style=True, width=1000).strip().removesuffix("...").strip()
    return f"`{rendered}`"


def _cell(text: str) -> str:
    return text.replace("|", "\\|")


def table(model: type[BaseModel], links: dict[type, tuple[str, str]], *, common: type | None = None) -> str:
    """A row per key. With ``common`` (the keys every source or channel has, documented once), only the keys
    ``model`` adds or redefines."""
    rows = ["| Key | Type | Default | Constraints | Description |", "|---|---|---|---|---|"]
    for name, field in model.model_fields.items():
        if common is not None and name in common.model_fields and name not in _own_fields(model):
            continue
        is_kind = name == "kind" and get_origin(field.annotation) is Literal
        cells = [
            f"`{field.alias or name}`",
            type_name(field.annotation, links, field.metadata),
            default(field),
            constraints(field),
            "Selects this kind." if is_kind else _cell(describe(model, name, field)),
        ]
        rows.append("| " + " | ".join(cells) + " |")
    return "\n".join(rows)


# --- the page ------------------------------------------------------------------------------------------------


def slug(text: str) -> str:
    """GitHub's heading id (what the handbook's renderer uses too)."""
    return re.sub(r"[^\w\- ]", "", text.lower()).replace(" ", "-")


@dataclass(frozen=True)
class Section:
    heading: str
    model: type[BaseModel]
    level: int = 2
    example: str = ""
    """A scout.yaml snippet (top-level keys), validated when the page renders."""
    common: type[BaseModel] | None = None
    """The keys every entry of this list has, documented in the parent section."""
    intro: str = ""
    entry_model: type[BaseModel] | None = None
    """For a source or channel kind: the example's single list entry is validated by this model."""


SECTIONS: list[Section] = [
    Section(
        "files",
        cfg.FilesConfig,
        example="""\
files:
  heartbeat: heartbeat.yaml
  approvals: approvals.yaml
  tariff: tariff.yaml
  signals: signals.yaml
  persona: persona.yaml
  memory: memory/
""",
        intro="Check each one with `argus-scout check <kind> FILE`, or all of them with `argus-scout check config`. "
        "The examples are in [`examples/`](../examples/).",
    ),
    Section("store", cfg.StoreConfig, example="store:\n  path: scout.db\n"),
    Section(
        "memory",
        cfg.MemoryConfig,
        example="memory:\n  write_rejections: false   # the memory directory is mounted read-only\n",
        intro="The directory itself is `files.memory`. Guide: [narrative.md](narrative.md#memory).",
    ),
    Section(
        "sources",
        cfg.SourceSettings,
        intro="A list. Every entry has these keys; each kind adds its own below. A bad entry fails only itself "
        "(and `argus-scout check config`): the other sources still load. Guide: [sources.md](sources.md).",
    ),
    Section(
        "sources: webhook",
        cfg.WebhookSource,
        level=3,
        common=cfg.SourceSettings,
        example="sources:\n  - kind: webhook\n    name: ci\n    token_env: SCOUT_CI_TOKEN\n",
        entry_model=cfg.WebhookSource,
    ),
    Section(
        "sources: json_poll",
        cfg.JsonPollSource,
        level=3,
        common=cfg.SourceSettings,
        example="""\
sources:
  - kind: json_poll
    name: alerts
    url: http://alertmanager:9093/api/v2/alerts
    interval: 60
    fields:
      summary: annotations.summary
      severity: labels.severity
    id: [labels.alertname, labels.instance]
""",
        entry_model=cfg.JsonPollSource,
    ),
    Section(
        "sources: suite_status",
        cfg.SuiteStatusSource,
        level=3,
        common=cfg.SourceSettings,
        intro="Guide: [suite.md](suite.md).",
        example="""\
sources:
  - kind: suite_status
    name: suite
    down_for: 120
    degraded_for: 600
    severity:
      down: critical
""",
        entry_model=cfg.SuiteStatusSource,
    ),
    Section("sources: suite_status: severity", cfg.SuiteSeverities, level=4),
    Section(
        "channels",
        cfg.ChannelSettings,
        intro="A list. Every entry has these keys; each kind adds its own below. A bad entry fails only itself: "
        "the other channels still load. Guide: [channels.md](channels.md).",
    ),
    Section(
        "channels: text",
        cfg.TextChannel,
        level=3,
        common=cfg.ChannelSettings,
        example="channels:\n  - kind: text\n    name: log\n    file: scout-{date}.log\n    rotate_bytes: 1048576\n",
        entry_model=cfg.TextChannel,
    ),
    Section(
        "channels: webhook",
        cfg.WebhookChannel,
        level=3,
        common=cfg.ChannelSettings,
        example="""\
channels:
  - kind: webhook
    name: chat
    url: https://hooks.example.com/scout
    secret_env: SCOUT_CHAT_SECRET
    kinds: [morning, evening]
""",
        entry_model=cfg.WebhookChannel,
    ),
    Section(
        "channels: snapcast",
        SnapcastConfig,
        level=3,
        common=cfg.ChannelSettings,
        intro='Needs the `snapcast` extra: `pip install "argus-scout[snapcast]"`.',
        example="""\
channels:
  - kind: snapcast
    name: house
    fifo: /run/snapcast/scout.fifo
    control: tcp://snapserver:1705
    stream: scout
    volume: 60
""",
        entry_model=SnapcastConfig,
    ),
    Section(
        "channels: mask",
        MaskConfig,
        level=3,
        common=cfg.ChannelSettings,
        intro='Needs the `mask` extra: `pip install "argus-scout[mask]"`.',
        example="channels:\n  - kind: mask\n    name: face\n    url: http://mask:8000\n    face: lobby\n",
        entry_model=MaskConfig,
    ),
    Section(
        "schedule",
        cfg.ScheduleConfig,
        example="""\
schedule:
  analysis_interval: 900
  status_interval: 300
  timezone: Europe/London
  briefings:
    - {kind: morning, at: "07:30"}
    - {kind: evening, at: "19:00"}
""",
    ),
    Section("schedule.briefings", cfg.Briefing, level=3),
    Section("services", cfg.ServicesConfig, example="services:\n  prometheus_url: http://prometheus:9090\n"),
    Section(
        "services.llm",
        cfg.LLMConfig,
        level=3,
        intro="Guide: [narrative.md](narrative.md).",
        example="""\
services:
  llm:
    backend: ollama
    base_url: http://ollama:11434
    model: qwen3:8b
""",
    ),
    Section(
        "services.llm.gpu",
        cfg.LLMGpuConfig,
        level=3,
        example="""\
services:
  llm:
    backend: openai
    base_url: http://vllm:8000/v1
    model: qwen3-8b
    gpu:
      target: vllm:qwen3-8b
""",
    ),
    Section(
        "services.echo",
        cfg.EchoConfig,
        level=3,
        example="services:\n  echo:\n    url: http://echo:8000\n    speaker: <SPEAKER>\n    format: pcm_s16le\n",
    ),
    Section("budget", cfg.BudgetConfig, example="budget:\n  limits:\n    llm_calls: 48\n    tts: 100\n"),
    Section(
        "delivery",
        cfg.DeliveryConfig,
        example="delivery:\n  retry_interval: 300\n  max_attempts: 5\n  speak_max_age: 600\n",
    ),
    Section(
        "server",
        cfg.ServerConfig,
        intro="Guide: [api.md](api.md).",
        example="""\
server:
  host: 127.0.0.1
  port: 8400
  read_only: false
  token_env: SCOUT_API_TOKEN
""",
    ),
]


def _links() -> dict[type, tuple[str, str]]:
    links = {s.model: (s.heading, slug(s.heading)) for s in SECTIONS}
    links[cfg.SourceSettings] = ("plugin kind", slug("Plugin kinds"))
    links[cfg.PluginSource] = ("plugin kind", slug("Plugin kinds"))
    links[cfg.PluginChannel] = ("plugin kind", slug("Plugin kinds"))
    return links


def _check_example(section: Section) -> None:
    data = yaml.safe_load(section.example)
    cfg.parse_config(data, Path("/config"), source=f"docs example for {section.heading}")
    if section.entry_model is not None:
        (entry,) = next(iter(data.values()))
        section.entry_model.model_validate(entry, context={"base_dir": Path("/config")})


INTRO = """\
# Config reference

Every key `scout.yaml` takes. This page is generated from the schema (`argus_scout.config`) by `make docs`, and a
test fails if it drifts, so edit the model's descriptions, not this file. For a commented starter, run
`argus-scout init`; to check a file, `argus-scout check config scout.yaml`.

## How the file is read

- **Strict.** An unknown key, at any level, is an error, and so is a bad value. Scout refuses to start on an
  invalid `scout.yaml` and `argus-scout check config` names each problem by its dotted path.
- **Forgiving below it.** The policy files under `files` are reloaded while scout runs, and a bad edit to one
  falls back to defaults (or skips the bad rule) and is logged, so a typo can't stop status updates.
- **Paths** that are relative resolve against the directory `scout.yaml` is in, so a config directory can be
  moved or mounted anywhere (the container mounts it at `/config`).
- **Secrets never go in the file.** A key ending in `_env` names an environment variable; the value lives in a
  `.env` beside `scout.yaml` or in the real environment, which wins. `check config` reports each one as set or
  unset, never its value.
- **Empty sections.** A section whose keys are all commented out reads as its defaults, and an empty list as `[]`.
- **Types.** *path*: a file path, relative to the config's directory. *env var name*: the name of a variable
  (`SCOUT_API_TOKEN`), never the secret. *URL*: `http://` or `https://`; a trailing slash is dropped. Durations are
  seconds.
"""

PLUGINS = """\
### Plugin kinds

`sources` and `channels` entries of any other `kind` belong to an installed plugin (entry-point groups
`argus_scout.sources` and `argus_scout.channels`). Here only `kind` and `name` are checked and every other key is
kept; the plugin's own `config_model` then validates the whole entry, with paths resolved against the config's
directory like the built-in kinds. An unknown kind, or an entry its plugin rejects, fails only that entry and is
reported by `argus-scout check config`. `snapcast` and `mask` are built-in plugin kinds of this sort, validated by
the models above. Writing one: [sources.md](sources.md#writing-a-source-plugin) and
[channels.md](channels.md#writing-a-channel-plugin).
"""

ENVIRONMENT = """\
## Environment

Not in `scout.yaml`, on purpose:

| Variable | Read by | Meaning |
|---|---|---|
| `SCOUT_CONFIG` | `argus-scout serve` | The config file when `--config` isn't given. The container image sets `/config/scout.yaml`. |
| the variables `*_env` keys name | scout | Secrets: tokens, API keys, HMAC keys. |
| `ARGUS_GPU_BROKER_URL`, `ARGUS_GPU_BROKER_TOKEN`, `ARGUS_GPU_COORDINATOR`, `ARGUS_GPU_LEASE_TIMEOUT_S`, ... | argus-cortex | The host-wide GPU coordinator every argus service on the host shares, used when `services.llm.gpu` is set. See [narrative.md](narrative.md#gpu-leases). |
| `ARGUS_XTTS_URL`, `ARGUS_TTS_URL`, `ARGUS_MASK_URL` | scout, through `argus_cortex.peers` | The suite-wide URL of an argus-echo engine or of argus-mask, read only when `services.echo` (with `peer`) or a `mask` channel leaves its `url` out. A value that isn't an absolute http(s) URL fails `check config`, naming the variable. |
| `ARGUS_ENV_FILE` | argus-cortex | The suite settings file: `~/.config/argus/suite.env` (mode 600), or `/etc/argus/suite.env` in a container. The `ARGUS_*` variables above may live there instead of in each service's `.env`; the environment and the `.env` beside `scout.yaml` win. `off` disables it. |
| `ARGUS_PEER`, `ARGUS_BUILD` | argus-cortex | What `/health` reports as `peer` (the container's `argus.peer` label, `scout`) and `build` (an image build id). Optional. |
"""


def render() -> str:
    links = _links()
    out = [INTRO, "## Top level\n", table(cfg.ScoutConfig, links), ""]
    for section in SECTIONS:
        out.append(f"{'#' * section.level} {section.heading}\n")
        doc = prose(section.model.__doc__)
        if doc and section.model.__doc__ != BaseModel.__doc__:
            out.append(doc + "\n")
        if section.intro:
            out.append(section.intro + "\n")
        out.append(table(section.model, links, common=section.common) + "\n")
        if section.example:
            _check_example(section)
            out.append(f"```yaml\n{section.example}```\n")
        if section.heading == "channels: mask":
            out.append(PLUGINS)
    out.append(ENVIRONMENT)
    return "\n".join(out).rstrip() + "\n"


def main(argv: list[str] | None = None) -> None:
    args = sys.argv[1:] if argv is None else argv
    target = Path(args[0]) if args else OUTPUT
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render())
    print(f"wrote {target}")


if __name__ == "__main__":
    main()
