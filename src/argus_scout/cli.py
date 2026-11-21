"""argus-scout command line."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import typer

import argus_scout
from argus_scout.approvals import load_rules
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
    host: str = typer.Option("127.0.0.1", help="Bind address. Scout has no auth yet: keep it on loopback."),
    port: int = typer.Option(8400),
) -> None:
    """Run the HTTP server (needs the [server] extra)."""
    try:
        import uvicorn
    except ImportError as exc:
        typer.echo("the server needs the [server] extra: pip install 'argus-scout[server]'", err=True)
        raise typer.Exit(1) from exc
    uvicorn.run("argus_scout.server.app:app", host=host, port=port)


if __name__ == "__main__":
    app()
