from __future__ import annotations

from pathlib import Path

from argus_cortex.net import FetchFailed, Reply
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from argus_scout.cli import app
from argus_scout.server.app import app as server
from argus_scout.sources import json_poll
from argus_scout.store import SqliteStore

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
runner = CliRunner()


def test_check_examples_pass():
    for kind in ("tariff", "heartbeat", "approvals"):
        result = runner.invoke(app, ["check", kind, str(EXAMPLES / f"{kind}.yaml")])
        assert result.exit_code == 0, result.output
        assert "ok:" in result.output


def test_check_tariff_reports_gaps(write_file):
    path = write_file("t.yaml", "plan: p\nperiods:\n  a:\n    start: '00:00'\n    end: '12:00'\n    rate: 1\n")
    result = runner.invoke(app, ["check", "tariff", str(path)])
    assert result.exit_code == 1
    assert "gap: no period covers 12:00-00:00" in result.output
    assert "ok:" not in result.output


def test_check_approvals_reports_bad_rules(write_file):
    path = write_file("a.yaml", "rules:\n  - id: x\n    scope: until\n")
    result = runner.invoke(app, ["check", "approvals", str(path)])
    assert result.exit_code == 1
    assert "needs expires_at" in result.output


def test_tariff_now():
    result = runner.invoke(app, ["tariff", "now", str(EXAMPLES / "tariff.yaml")])
    assert result.exit_code == 0, result.output
    assert "next:" in result.output


def test_health():
    assert TestClient(server).get("/health").json()["status"] == "ok"


POLL_CONFIG = """\
store: {path: scout.db}
sources:
  - {kind: webhook, name: ci}
  - kind: json_poll
    name: alerts
    url: http://status.lan:9093/alerts
    items: alerts
    fields: {summary: name, severity: level}
  - {kind: json_poll, name: down, url: "http://down.lan/"}
  - {kind: carrier_pigeon, name: coop}
"""


def _stub_fetch(monkeypatch):
    async def fetch(url, **kwargs):
        if "down" in url:
            raise FetchFailed(503, "down.lan")
        return Reply(200, {}, b'{"alerts": [{"name": "DiskFull", "level": "critical"}]}', url)

    monkeypatch.setattr(json_poll, "afetch", fetch)


def test_poll_dry_run_reports_each_source(write_file, monkeypatch):
    _stub_fetch(monkeypatch)
    path = write_file("scout.yaml", POLL_CONFIG)
    result = runner.invoke(app, ["poll", "--config", str(path)])
    assert result.exit_code == 1  # one source failed and one kind is unknown, but the rest still polled
    assert "source alerts: 1 events" in result.output
    assert "[critical] alerts/alerts:" in result.output and "DiskFull" in result.output
    assert "source down: error: down: down.lan answered 503" in result.output
    assert "source coop: error: unknown source kind 'carrier_pigeon'" in result.output
    assert "dry run" in result.output
    assert not (path.parent / "scout.db").exists()


def test_poll_one_source_and_ingest(write_file, monkeypatch):
    _stub_fetch(monkeypatch)
    path = write_file("scout.yaml", POLL_CONFIG)
    first = runner.invoke(app, ["poll", "-c", str(path), "--source", "alerts", "--ingest"])
    assert first.exit_code == 0, first.output
    assert "(new)" in first.output
    again = runner.invoke(app, ["poll", "-c", str(path), "--source", "alerts", "--ingest"])
    assert "(refreshed)" in again.output
    with SqliteStore(path.parent / "scout.db") as store:
        assert [e.summary for e in store.open_events()] == ["DiskFull"]


def test_poll_refuses_push_and_unknown_sources(write_file):
    path = write_file("scout.yaml", POLL_CONFIG)
    push = runner.invoke(app, ["poll", "-c", str(path), "--source", "ci"])
    assert push.exit_code == 1 and "pushed to, not polled" in push.output
    missing = runner.invoke(app, ["poll", "-c", str(path), "--source", "nope"])
    assert missing.exit_code == 1 and "no source by that name" in missing.output


def test_check_config_reports_bad_sources(write_file):
    path = write_file("scout.yaml", POLL_CONFIG)
    result = runner.invoke(app, ["check", "config", str(path)])
    assert result.exit_code == 1
    assert "source coop: error: unknown source kind" in result.output
