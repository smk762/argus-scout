"""The container image must start something that exists: its CMD/ENTRYPOINT names a real target."""

from __future__ import annotations

import importlib
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
