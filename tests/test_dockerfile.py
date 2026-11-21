"""The container image must start something that exists: its CMD/ENTRYPOINT names a real target."""

from __future__ import annotations

import importlib
import inspect
import json
import re
import tomllib
from pathlib import Path

import typer

ROOT = Path(__file__).resolve().parent.parent


def _instructions() -> dict[str, list[str]]:
    """The last ``CMD``/``ENTRYPOINT`` (exec form) and every ``ENV`` line of the Dockerfile."""
    text = re.sub(r"\\\n", " ", (ROOT / "Dockerfile").read_text())
    found: dict[str, list[str]] = {"ENV": []}
    for line in text.splitlines():
        match = re.match(r"\s*(CMD|ENTRYPOINT|ENV)\s+(.*)", line)
        if not match:
            continue
        word, rest = match.groups()
        if word == "ENV":
            found["ENV"].append(rest.strip())
            continue
        assert rest.lstrip().startswith("["), f"{word} must use the exec (JSON) form: {rest}"
        found[word] = json.loads(rest)
    return found


def _command() -> list[str]:
    found = _instructions()
    command = found.get("ENTRYPOINT", []) + found.get("CMD", [])
    assert command, "the Dockerfile has no CMD or ENTRYPOINT"
    return command


def _resolve(target: str):
    module, _, attr = target.partition(":")
    obj = importlib.import_module(module)
    for part in attr.split("."):
        obj = getattr(obj, part)
    return obj


def test_image_command_targets_exist():
    command = _command()
    scripts = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["scripts"]
    if command[0] in scripts:
        app = _resolve(scripts[command[0]])
        group = typer.main.get_command(app)
        commands = group.commands  # type: ignore[attr-defined]
        assert command[1] in commands, f"{command[0]} has no {command[1]!r} command"
        # Parse the arguments the image passes, so a renamed option breaks here rather than in the container.
        sub = commands[command[1]]
        ctx = sub.make_context(command[1], list(command[2:]), resilient_parsing=True)
        assert ctx.params.get("host") == "0.0.0.0"
    elif command[0] == "uvicorn":
        target = next(arg for arg in command[1:] if not arg.startswith("-") and ":" in arg)
        assert _resolve(target) is not None
    else:
        raise AssertionError(f"unrecognised image command {command[0]!r}: teach this test to check it")


def test_image_config_env_matches_the_serve_option():
    env = " ".join(_instructions()["ENV"])
    assert "SCOUT_CONFIG=/config/scout.yaml" in env
    from argus_scout.cli import app

    serve = typer.main.get_command(app).commands["serve"]  # type: ignore[attr-defined]
    [option] = [p for p in serve.params if p.name == "config_path"]
    assert option.envvar == "SCOUT_CONFIG"


def _image_extras() -> set[str]:
    """The extras the image's ``pip install`` of the project names (``".[a,b]"`` -> ``{"a", "b"}``)."""
    text = re.sub(r"\\\n", " ", (ROOT / "Dockerfile").read_text())
    found: set[str] = set()
    for line in text.splitlines():
        if re.match(r"\s*RUN\s", line) and "pip install" in line:
            for group in re.findall(r"""["']?\.\[([^\]]*)\]""", line):
                found |= {extra.strip() for extra in group.split(",") if extra.strip()}
    assert found, "the Dockerfile installs the project with no extras (no `.[...]` in a pip install)"
    return found


def _builtin_extras() -> dict[str, str]:
    """``kind -> extra`` for every built-in channel and source kind that needs one."""
    from argus_scout.channels.registry import BUILTIN_EXTRAS
    from argus_scout.sources.registry import BUILTINS as SOURCE_BUILTINS

    needed = {f"channel {kind}": extra for kind, extra in BUILTIN_EXTRAS.items()}
    for kind, plugin in SOURCE_BUILTINS.items():
        extra = getattr(inspect.getmodule(plugin), "EXTRA", None)
        if extra:
            needed[f"source {kind}"] = extra
    return needed


def test_builtin_extras_mapping_is_complete():
    """A built-in channel whose module declares ``EXTRA`` is in ``BUILTIN_EXTRAS``, with that extra."""
    from argus_scout.channels.registry import BUILTIN_EXTRAS, BUILTINS

    declared = {
        kind: extra for kind, cls in BUILTINS.items() if (extra := getattr(inspect.getmodule(cls), "EXTRA", None))
    }
    assert declared == BUILTIN_EXTRAS


def test_image_installs_every_builtin_extra():
    """Every extra behind a built-in channel or source kind is a real extra, and the image installs it."""
    optional = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["optional-dependencies"]
    needed = _builtin_extras()
    assert needed, "no built-in kind needs an extra: drop this test or teach it the new mapping"
    unknown = {kind: extra for kind, extra in needed.items() if extra not in optional}
    assert not unknown, f"built-ins name extras pyproject.toml doesn't define: {unknown}"
    installed = _image_extras()
    missing = {kind: extra for kind, extra in needed.items() if extra not in installed}
    assert not missing, f"the image doesn't install these built-ins' extras (add them to the Dockerfile): {missing}"
    stray = installed - set(optional)
    assert not stray, f"the Dockerfile installs extras pyproject.toml doesn't define: {stray}"
