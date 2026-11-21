"""argus-scout command line."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from importlib.resources import files as package_files
from pathlib import Path

import typer

import argus_scout
from argus_scout.approvals import load_rules
from argus_scout.config import CONFIG_FILE, ENV_FILE, ConfigError, ScoutConfig, load_config, resolve_secret
from argus_scout.heartbeat import PolicyFile
from argus_scout.tariff import coverage_gaps, current_period, load_tariff, next_period_change

app = typer.Typer(
    help="Homelab intelligence: reads your telemetry and reports back. It never acts.", no_args_is_help=True
)
check_app = typer.Typer(help="Validate config files before scout loads them.", no_args_is_help=True)
tariff_app = typer.Typer(help="Inspect a time-of-use tariff.", no_args_is_help=True)
app.add_typer(check_app, name="check")
app.add_typer(tariff_app, name="tariff")

ExistingFile = typer.Argument(..., exists=True, dir_okay=False, readable=True)


@app.command()
def version() -> None:
    """Print the installed version."""
    typer.echo(argus_scout.__version__)


@check_app.command("tariff")
def check_tariff(path: Path = ExistingFile) -> None:
    """Check a tariff file loads and its periods cover the whole day."""
    try:
        tariff = load_tariff(path)
    except Exception as exc:  # noqa: BLE001 — report any load error as a failed check
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc
    gaps = coverage_gaps(tariff.periods)
    for start, end in gaps:
        typer.echo(f"gap: no period covers {start:%H:%M}-{end:%H:%M}", err=True)
    summary = f"plan {tariff.plan.name}, {len(tariff.periods)} periods, {len(tariff.comparison_plans)} comparison plans"
    typer.echo(f"ok: {summary}" if not gaps else f"failed: {summary}, {len(gaps)} gaps")
    if gaps:
        raise typer.Exit(1)


@check_app.command("heartbeat")
def check_heartbeat(path: Path = ExistingFile) -> None:
    """Check a heartbeat policy file parses, and show which keys it overrides."""
    policy = PolicyFile(path).get()
    if policy.file_error:
        typer.echo(f"error: {policy.file_error}", err=True)
        raise typer.Exit(1)
    overridden = sorted(k for k, v in policy.origin.items() if v == "file")
    typer.echo(f"ok: overrides {', '.join(overridden) or 'nothing'}; {len(policy.sources)} source rules")


@check_app.command("approvals")
def check_approvals(path: Path = ExistingFile) -> None:
    """Check a standing-approvals file: every rule must parse."""
    try:
        rules, errors = load_rules(path)
    except Exception as exc:  # noqa: BLE001
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc
    for error in errors:
        typer.echo(f"error: {error}", err=True)
    typer.echo(f"ok: {len(rules)} rules" if not errors else f"{len(rules)} rules loaded, {len(errors)} skipped")
    if errors:
        raise typer.Exit(1)


@check_app.command("config")
def check_config(path: Path = ExistingFile) -> None:
    """Check scout.yaml and every policy file it names. Secrets are reported as set or unset, never shown."""
    try:
        config = load_config(path)
    except ConfigError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc
    failed = False
    checks: dict[str, Callable[[Path], None]] = {
        "heartbeat": check_heartbeat,
        "approvals": check_approvals,
        "tariff": check_tariff,
    }
    for kind, check in checks.items():
        named = getattr(config.files, kind)
        if named is None:
            typer.echo(f"{kind}: not configured, using defaults")
            continue
        if not named.is_file():
            typer.echo(f"{kind}: error: {named} does not exist", err=True)
            failed = True
            continue
        typer.echo(f"{kind}: ", nl=False)
        try:
            check(named)
        except typer.Exit as exc:
            failed = failed or bool(exc.exit_code)
    for name in config.secret_env_names():
        typer.echo(f"secret {name}: {'set' if resolve_secret(name) else 'unset'}")
    summary = _config_summary(config)
    typer.echo(f"failed: {summary}" if failed else f"ok: {summary}")
    if failed:
        raise typer.Exit(1)


def _config_summary(config: ScoutConfig) -> str:
    return (
        f"{len(config.sources)} sources, {len(config.schedule.briefings)} briefings, "
        f"store {config.store.path}, server {config.server.host}:{config.server.port}"
    )


@app.command()
def init(
    path: Path = typer.Argument(
        Path(CONFIG_FILE), help="Config file (.yaml/.yml) to write, or a directory to write scout.yaml in."
    ),
    force: bool = typer.Option(False, "--force", help="Overwrite existing files."),
) -> None:
    """Write a commented starter scout.yaml and a .env.example beside it."""
    if path.is_dir() or (not path.exists() and path.suffix not in (".yaml", ".yml")):
        path = path / CONFIG_FILE
    env_example = path.parent / f"{ENV_FILE}.example"
    targets = {path: "scout.yaml", env_example: "env.example"}
    existing = [str(target) for target in targets if target.exists()]
    if existing and not force:
        typer.echo(f"error: {', '.join(existing)} already exists; pass --force to overwrite", err=True)
        raise typer.Exit(1)
    path.parent.mkdir(parents=True, exist_ok=True)
    templates = package_files("argus_scout") / "examples"
    for target, template in targets.items():
        target.write_text((templates / template).read_text())
        typer.echo(f"wrote {target}")
    typer.echo(f"next: copy {env_example.name} to {ENV_FILE}, edit both, then: argus-scout check config {path}")


@tariff_app.command("now")
def tariff_now(path: Path = ExistingFile) -> None:
    """Show the active period and when it next changes."""
    tariff = load_tariff(path)
    now = datetime.now()
    period = current_period(tariff, now)
    name, seconds = next_period_change(tariff, now)
    hours, minutes = divmod(seconds // 60, 60)
    typer.echo(f"{period.name}: {period.rate:g} per {tariff.unit} ({tariff.currency} minor units)")
    typer.echo(f"next: {name} in {hours}h{minutes:02d}m")


@app.command()
def serve(
    config_path: Path | None = typer.Option(
        None, "--config", "-c", exists=True, dir_okay=False, help="scout.yaml; refuses to start if it is invalid."
    ),
    host: str | None = typer.Option(
        None, help="Bind address [default: server.host, else 127.0.0.1]. Scout has no auth yet: keep it on loopback."
    ),
    port: int | None = typer.Option(None, help="[default: server.port, else 8400]"),
) -> None:
    """Run the HTTP server (needs the [server] extra)."""
    config = ScoutConfig()
    if config_path is not None:
        try:
            config = load_config(config_path)
        except ConfigError as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(1) from exc
    host = host or config.server.host
    port = port or config.server.port
    try:
        import uvicorn
    except ImportError as exc:
        typer.echo("the server needs the [server] extra: pip install 'argus-scout[server]'", err=True)
        raise typer.Exit(1) from exc
    uvicorn.run("argus_scout.server.app:app", host=host, port=port)


if __name__ == "__main__":
    app()
