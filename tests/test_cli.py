from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
from typer.testing import CliRunner

from argus_scout.cli import app
from argus_scout.server.app import app as server

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
