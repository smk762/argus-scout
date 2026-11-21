"""argus-scout command line."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime
from importlib.resources import files as package_files
from pathlib import Path

import typer

import argus_scout
from argus_scout.approvals import StandingApprovals, load_rules
from argus_scout.config import CONFIG_FILE, ENV_FILE, ConfigError, ScoutConfig, load_config, resolve_secret
from argus_scout.engine import RecommendationEngine
from argus_scout.heartbeat import PolicyFile
from argus_scout.inbox import Inbox, InvalidEvent
from argus_scout.prometheus import PrometheusClient
from argus_scout.signals import Evaluation, SignalState, evaluate, load_signal_rules
from argus_scout.sources import build_sources
from argus_scout.store import SqliteStore
from argus_scout.tariff import TariffFile, coverage_gaps, current_period, load_tariff, next_period_change
from argus_scout.tariff_rules import evaluate_tariff

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


@check_app.command("signals")
def check_signals(path: Path = ExistingFile) -> None:
    """Check a signal rules file: every rule (threshold, tariff_shift, tariff_plan) must parse, and its templates
    use only allowed placeholders."""
    ruleset = load_signal_rules(path)
    if ruleset.file_error:
        typer.echo(f"error: {ruleset.file_error}", err=True)
        raise typer.Exit(1)
    for error in ruleset.errors:
        typer.echo(f"error: {error}", err=True)
    for rule in ruleset.rules:
        typer.echo(f"  {rule.name}: {rule.summary}")
    count = len(ruleset.rules)
    typer.echo(f"ok: {count} rules" if not ruleset.errors else f"{count} rules loaded, {len(ruleset.errors)} problems")
    if ruleset.errors:
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
        "signals": check_signals,
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
    for name, reason in build_sources(config).errors.items():
        typer.echo(f"source {name}: error: {reason}", err=True)
        failed = True
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
def poll(
    config_path: Path = typer.Option(..., "--config", "-c", exists=True, dir_okay=False, help="scout.yaml"),
    source: str | None = typer.Option(
        None, "--source", "-s", help="Poll only this source [default: every polling source]."
    ),
    ingest: bool = typer.Option(False, "--ingest", help="Write the events to the store instead of only printing them."),
) -> None:
    """Poll sources once and print the events they return. A dry run unless --ingest is given."""
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc
    built = build_sources(config)
    if source is not None:
        bound = built.get(source)
        if bound is None:
            reason = built.errors.get(source, "no source by that name")
            typer.echo(f"error: source {source}: {reason}", err=True)
            raise typer.Exit(1)
        if not bound.polls:
            typer.echo(f"error: source {source} is a {bound.kind} source; it is pushed to, not polled", err=True)
            raise typer.Exit(1)
        targets = [bound]
    else:
        targets = built.polling
        for name, reason in built.errors.items():
            typer.echo(f"source {name}: error: {reason}", err=True)
    if not targets:
        typer.echo("no polling sources configured")
        raise typer.Exit(1 if built.errors else 0)
    inbox = Inbox(SqliteStore(config.store.path)) if ingest else None
    failed = bool(built.errors) and source is None
    try:
        for bound in targets:
            try:
                events = asyncio.run(bound.plugin.poll(bound.config))  # type: ignore[attr-defined]
            except Exception as exc:  # noqa: BLE001 — one failing source doesn't stop the others
                typer.echo(f"source {bound.name}: error: {exc}", err=True)
                failed = True
                continue
            typer.echo(f"source {bound.name}: {len(events)} events")
            for event in events:
                line = f"  [{event.severity.value}] {event.source}/{event.event_id}: {event.summary}"
                if inbox is not None:
                    try:
                        line = f"{line} ({inbox.ingest(event).outcome.value})"
                    except InvalidEvent as exc:
                        line, failed = f"{line} (refused: {exc})", True
                typer.echo(line)
    finally:
        if inbox is not None:
            inbox.store.close()
    if not ingest:
        typer.echo("dry run: nothing was written (pass --ingest to store these)")
    if failed:
        raise typer.Exit(1)


@app.command()
def analyze(
    config_path: Path = typer.Option(..., "--config", "-c", exists=True, dir_okay=False, help="scout.yaml"),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Work on an in-memory copy of the store: show what would change, write nothing."
    ),
) -> None:
    """Run one signal evaluation and engine cycle against the configured Prometheus, and print the report.

    For testing rules; the server runs the same cycle on its schedule. Scout only proposes and (by
    standing rule) approves: nothing here deploys or acts.
    """
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc
    if not config.services.prometheus_url:
        typer.echo("error: services.prometheus_url is not set", err=True)
        raise typer.Exit(1)
    if config.files.signals is None:
        typer.echo("error: files.signals is not set", err=True)
        raise typer.Exit(1)
    ruleset = load_signal_rules(config.files.signals)
    if ruleset.file_error:
        typer.echo(f"error: {config.files.signals}: {ruleset.file_error}", err=True)
        raise typer.Exit(1)
    for error in ruleset.errors:
        typer.echo(f"signals: skipped {error}", err=True)
    store = SqliteStore.copy_of(config.store.path) if dry_run else SqliteStore(config.store.path)
    try:
        approvals = StandingApprovals(config.files.approvals, store=store)
        engine = RecommendationEngine(store, approvals)
        client = PrometheusClient(config.services.prometheus_url)
        state, now, tariff = SignalState(store), engine.clock(), TariffFile(config.files.tariff)

        async def run() -> Evaluation:
            hits = await evaluate(ruleset.rules, client, now, state=state)
            tariff_hits = await evaluate_tariff(
                ruleset.rules, client, tariff.get(), now, tz=config.schedule.tz, state=state, tariff_error=tariff.error
            )
            return hits.merge(tariff_hits)

        hits = asyncio.run(run())
        report = engine.run_cycle(hits)
    finally:
        store.close()
    typer.echo(f"{len(ruleset.rules)} rules: {len(hits)} firing, {hits.pending} pending")
    for line in report.lines():
        typer.echo(f"  {line}")
    typer.echo(report.summary())
    if dry_run:
        typer.echo("dry run: nothing was written")
    if report.unknown or ruleset.errors:
        raise typer.Exit(1)


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
    """Run the HTTP server and the scheduler (needs the [server] extra)."""
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

        from argus_scout.server.app import create_app
    except ImportError as exc:
        typer.echo("the server needs the [server] extra: pip install 'argus-scout[server]'", err=True)
        raise typer.Exit(1) from exc
    uvicorn.run(create_app(config), host=host, port=port)


if __name__ == "__main__":
    app()
