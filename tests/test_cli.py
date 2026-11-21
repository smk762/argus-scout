from __future__ import annotations

from pathlib import Path

from argus_cortex.net import FetchFailed, Reply
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from argus_scout import prometheus
from argus_scout.cli import app
from argus_scout.server.app import create_app
from argus_scout.sources import json_poll
from argus_scout.store import SqliteStore

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
runner = CliRunner()


def test_check_examples_pass():
    for kind in ("tariff", "heartbeat", "approvals", "signals"):
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
    assert TestClient(create_app()).get("/health").json()["status"] == "ok"


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


def test_check_signals_reports_bad_rules(write_file):
    path = write_file(
        "s.yaml",
        "rules:\n  - {name: ok, query: up, threshold: 0, recommendation: {title: t, action: a}}\n"
        "  - {name: bad, query: up, threshold: 0, recommendation: {title: '{x.y}', action: a}}\n",
    )
    result = runner.invoke(app, ["check", "signals", str(path)])
    assert result.exit_code == 1
    assert "rule 1 (bad)" in result.output and "no attribute or index access" in result.output
    assert "1 rules loaded, 1 problems" in result.output


ANALYZE_CONFIG = """\
store: {path: scout.db}
files: {signals: signals.yaml}
services: {prometheus_url: "http://prometheus.lan:9090"}
"""

SIGNALS = """\
rules:
  - name: disk-filling
    query: disk_used_percent
    threshold: 85
    labels_key: [instance]
    recommendation: {title: "{instance} is {value:.0f}% full", action: free space}
"""


def _stub_prometheus(monkeypatch, status=200):
    calls = []

    async def fetch(url, **kwargs):
        calls.append(url)
        body = (
            b'{"status":"success","data":{"resultType":"vector","result":'
            b'[{"metric":{"instance":"node-a:9100"},"value":[1700000000,"91"]}]}}'
        )
        return Reply(status, {}, body, url)

    monkeypatch.setattr(prometheus, "afetch", fetch)
    return calls


def test_analyze_dry_run_writes_nothing(write_file, monkeypatch):
    calls = _stub_prometheus(monkeypatch)
    write_file("signals.yaml", SIGNALS)
    path = write_file("scout.yaml", ANALYZE_CONFIG)
    result = runner.invoke(app, ["analyze", "--config", str(path), "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "new [warning] node-a:9100 is 91% full (proposed)" in result.output
    assert "dry run" in result.output
    assert all(url.startswith("http://prometheus.lan:9090/api/v1/query?") for url in calls)
    assert not (path.parent / "scout.db").exists()


def test_analyze_writes_and_dedups(write_file, monkeypatch):
    _stub_prometheus(monkeypatch)
    write_file("signals.yaml", SIGNALS)
    path = write_file("scout.yaml", ANALYZE_CONFIG)
    assert runner.invoke(app, ["analyze", "-c", str(path)]).exit_code == 0
    again = runner.invoke(app, ["analyze", "-c", str(path)])
    assert "refreshed [warning]" in again.output
    with SqliteStore(path.parent / "scout.db") as store:
        assert len(store.list_recommendations()) == 1


def test_analyze_reports_an_outage_as_unknown(write_file, monkeypatch):
    _stub_prometheus(monkeypatch, status=503)
    write_file("signals.yaml", SIGNALS)
    path = write_file("scout.yaml", ANALYZE_CONFIG)
    result = runner.invoke(app, ["analyze", "-c", str(path), "--dry-run"])
    assert result.exit_code == 1
    assert "unknown disk-filling: prometheus:" in result.output


def test_analyze_needs_prometheus_and_signals(write_file):
    path = write_file("scout.yaml", "store: {path: scout.db}\n")
    result = runner.invoke(app, ["analyze", "-c", str(path)])
    assert result.exit_code == 1 and "prometheus_url is not set" in result.output
