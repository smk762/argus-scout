from __future__ import annotations

from importlib.resources import files as package_files
from pathlib import Path

import pytest
from typer.testing import CliRunner

from argus_scout.cli import app
from argus_scout.config import (
    ConfigError,
    JsonPollSource,
    ScoutConfig,
    WebhookSource,
    load_config,
    parse_config,
    parse_env_file,
    resolve_secret,
)

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
STARTER = package_files("argus_scout") / "examples" / "scout.yaml"
SECRET = "s3cret-value-never-shown"
runner = CliRunner()


@pytest.fixture
def unset_env(monkeypatch):
    """Unset env vars and remove them again after the test, even ones a .env load sets."""

    def unset(*names: str) -> None:
        for name in names:
            monkeypatch.setenv(name, "")  # records the original, so teardown restores (or deletes) it
            monkeypatch.delenv(name)

    return unset


def test_starter_loads_with_defaults(tmp_path):
    path = tmp_path / "scout.yaml"
    path.write_text(STARTER.read_text())
    config = load_config(path, load_env=False)
    assert config.files.heartbeat is None
    assert config.store.path == tmp_path / "scout.db"
    assert config.sources == []
    assert [b.kind for b in config.schedule.briefings] == ["morning", "evening"]
    assert config.server.read_only is True
    assert config.server.host == "127.0.0.1"


def test_full_config(write_file):
    path = write_file(
        "scout.yaml",
        """
files:
  heartbeat: policy/heartbeat.yaml
  tariff: /etc/scout/tariff.yaml
store:
  path: data/scout.db
sources:
  - kind: webhook
    name: ci
    token_env: SCOUT_CI_TOKEN
  - kind: json_poll
    name: backups
    url: http://backups:8080/status/
    interval: 30
    items: jobs
    fields: {summary: message, severity: level}
    auth_env: SCOUT_BACKUPS_AUTH
schedule:
  timezone: Europe/London
  briefings: [{kind: morning, at: "07:30"}]
services:
  prometheus_url: http://prometheus:9090
  llm: {backend: ollama, base_url: "http://ollama:11434", model: some-model}
""",
    )
    config = load_config(path, load_env=False)
    base = path.parent
    assert config.files.heartbeat == base / "policy" / "heartbeat.yaml"
    assert config.files.tariff == Path("/etc/scout/tariff.yaml")
    assert config.store.path == base / "data" / "scout.db"
    webhook, poll = config.sources
    assert isinstance(webhook, WebhookSource)
    assert isinstance(poll, JsonPollSource)
    assert poll.url == "http://backups:8080/status"
    assert poll.fields == {"summary": "message", "severity": "level"}
    assert config.schedule.briefings[0].at_time.hour == 7
    assert config.services.prometheus_url == "http://prometheus:9090"
    assert config.secret_env_names() == ["SCOUT_API_TOKEN", "SCOUT_CI_TOKEN", "SCOUT_BACKUPS_AUTH"]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("serve: {}\n", "serve: unknown key"),
        ("server: {hots: x}\n", "server.hots: unknown key"),
        ("sources: [{kind: webhook, name: a, extra: 1}]\n", "unknown key"),
        ("sources: [{kind: carrier_pigeon}]\n", "sources.0.plugin.name: Field required"),
        ("sources: [{name: a}]\n", "sources.0.plugin.kind: Field required"),
        ("sources: [{kind: webhook, name: a}, {kind: webhook, name: a}]\n", "duplicate source name"),
        ("sources: [{kind: json_poll, name: a, url: http://x, fields: {colour: c}}]\n", "not status event fields"),
        ("sources: [{kind: json_poll, name: a, url: ftp://x}]\n", "not an http(s) URL"),
        ("schedule: {briefings: [{kind: morning, at: '7:30'}]}\n", "HH:MM"),
        ("schedule: {timezone: Mars/Olympus}\n", "unknown timezone"),
        (
            "schedule: {briefings: [{kind: morning, at: '07:30'}, {kind: morning, at: '07:30'}]}\n",
            "duplicate briefing: morning at 07:30",
        ),
        ("sources: [{kind: [webhook], name: a}]\n", "sources.0.plugin.kind: Input should be a valid string"),
        ("sources: [{kind: {x: 1}, name: a}]\n", "sources.0.plugin.kind: Input should be a valid string"),
        (
            "sources: [{kind: json_poll, name: a, url: http://x, fields: {source: s}}]\n",
            "source can't be mapped",
        ),
        (
            "sources: [{kind: json_poll, name: a, url: http://status.lan, id: k, fields: {event_id: k}}]\n",
            "set id or fields.event_id, not both",
        ),
        ("sources: [{kind: json_poll, name: a, url: http://x, id: []}]\n", "non-empty list"),
        ("server: {token_env: 'hunter2 is my password'}\n", "not an environment variable name"),
        ("services: {echo: {url: 'http://echo:80x'}}\n", "not a valid http(s) URL"),
        ("services: {prometheus_url: 'http://prom:99999'}\n", "not a valid http(s) URL"),
        ("services: {llm: {base_url: 'http://llm:-1/v1', model: m}}\n", "not a valid http(s) URL"),
        ("sources: [{kind: json_poll, name: a, url: 'http://backups:8o8o/status'}]\n", "not a valid http(s) URL"),
        ("channels: [{kind: webhook, name: c, url: 'https://hooks:70000/x'}]\n", "not a valid http(s) URL"),
        ("channels: [{kind: webhook, name: c, url: 'https://:8080/x'}]\n", "not an http(s) URL"),
        ("- a list\n", "expected a mapping"),
        ("server: [unclosed\n", "invalid YAML"),
    ],
)
def test_invalid_config_raises(write_file, text, expected):
    path = write_file("scout.yaml", text)
    with pytest.raises(ConfigError, match=expected.replace("(", r"\(").replace(")", r"\)")):
        load_config(path, load_env=False)


def test_missing_config_raises(tmp_path):
    with pytest.raises(ConfigError, match="cannot read config"):
        load_config(tmp_path / "nope.yaml")


def test_entries_commented_out_at_any_depth_read_as_empty(write_file):
    text = """
schedule:
  briefings:
    # - {kind: morning, at: "07:30"}
server:
  allowed_origins:
    # - https://ui.example
budget:
  limits:
    # llm_calls: 100
sources:
  - kind: json_poll
    name: poll
    url: http://status.lan/
    fields:
      # summary: message
files:
"""
    config = load_config(write_file("scout.yaml", text), load_env=False)
    assert config.schedule.briefings == [] and config.server.allowed_origins == []
    assert config.budget.limits == {} and config.sources[0].fields == {}
    assert config.files.heartbeat is None


def test_empty_config_is_all_defaults(write_file):
    config = load_config(write_file("scout.yaml", ""), load_env=False)
    assert config.server.port == 8400
    assert config.store.path.name == "scout.db"


def test_secrets_resolve_from_dotenv_and_never_appear(write_file, unset_env, monkeypatch):
    unset_env("SCOUT_API_TOKEN", "SCOUT_LLM_KEY", "SCOUT_ALREADY_SET")
    monkeypatch.setenv("SCOUT_ALREADY_SET", "from-environment")
    llm = '{backend: openai, base_url: "https://llm/v1", model: m, api_key_env: SCOUT_LLM_KEY}'
    path = write_file("scout.yaml", f"services:\n  llm: {llm}\n")
    write_file(
        ".env",
        f"# comment\nSCOUT_API_TOKEN={SECRET}\nexport SCOUT_LLM_KEY='quoted value'\nSCOUT_ALREADY_SET=from-file\n",
    )
    config = load_config(path)

    token = resolve_secret(config.server.token_env)
    assert token is not None and token.get_secret_value() == SECRET
    assert resolve_secret("SCOUT_LLM_KEY").get_secret_value() == "quoted value"
    assert resolve_secret("SCOUT_ALREADY_SET").get_secret_value() == "from-environment"  # real env wins
    assert resolve_secret(None) is None
    assert resolve_secret("SCOUT_DEFINITELY_UNSET_VAR") is None

    for text in (repr(config), str(config), config.model_dump_json(), repr(config.model_dump()), repr(token)):
        assert SECRET not in text


def test_a_secret_where_its_env_name_belongs_never_appears_in_the_error():
    with pytest.raises(ConfigError) as caught:
        parse_config({"server": {"token_env": SECRET}})
    assert "server.token_env" in str(caught.value) and "not an environment variable name" in str(caught.value)
    for text in (str(caught.value), str(caught.value.__cause__), repr(caught.value.__cause__)):
        assert SECRET not in text


def test_parse_env_file(tmp_path):
    path = tmp_path / ".env"
    path.write_text('A=1\n\n# no\nB = "two words"\nC=plain # trailing comment\nnot a line\n1BAD=x\nD=\n')
    assert parse_env_file(path) == {"A": "1", "B": "two words", "C": "plain", "D": ""}


def test_config_is_immutable():
    config = ScoutConfig()
    with pytest.raises(ValueError):
        config.server.port = 1  # type: ignore[misc]


# --- CLI ---------------------------------------------------------------------------------------------------


def test_init_writes_refuses_and_forces(tmp_path):
    target = tmp_path / "conf" / "scout.yaml"
    result = runner.invoke(app, ["init", str(target)])
    assert result.exit_code == 0, result.output
    assert target.read_text() == STARTER.read_text()
    assert "SCOUT_API_TOKEN=" in (target.parent / ".env.example").read_text()

    target.write_text("# my edits\n")
    result = runner.invoke(app, ["init", str(target)])
    assert result.exit_code == 1
    assert "already exists" in result.output
    assert target.read_text() == "# my edits\n"

    result = runner.invoke(app, ["init", str(target), "--force"])
    assert result.exit_code == 0, result.output
    assert target.read_text() == STARTER.read_text()


def test_init_into_directory_then_check(tmp_path, unset_env):
    unset_env("SCOUT_API_TOKEN")
    assert runner.invoke(app, ["init", str(tmp_path)]).exit_code == 0
    result = runner.invoke(app, ["check", "config", str(tmp_path / "scout.yaml")])
    assert result.exit_code == 0, result.output
    assert "heartbeat: not configured" in result.output
    assert "secret SCOUT_API_TOKEN: unset" in result.output
    assert "ok:" in result.output


def test_check_config_checks_named_files(write_file, unset_env):
    unset_env("SCOUT_API_TOKEN")
    write_file(".env", f"SCOUT_API_TOKEN={SECRET}\n")
    files = "".join(f"  {kind}: {EXAMPLES / f'{kind}.yaml'}\n" for kind in ("heartbeat", "approvals", "tariff"))
    path = write_file("scout.yaml", f"files:\n{files}")
    result = runner.invoke(app, ["check", "config", str(path)])
    assert result.exit_code == 0, result.output
    assert "tariff: ok: plan" in result.output
    assert "secret SCOUT_API_TOKEN: set" in result.output
    assert SECRET not in result.output


def test_check_config_fails_on_missing_or_bad_file(write_file):
    write_file("approvals.yaml", "rules:\n  - id: x\n    scope: until\n")
    path = write_file("scout.yaml", "files:\n  heartbeat: missing.yaml\n  approvals: approvals.yaml\n")
    result = runner.invoke(app, ["check", "config", str(path)])
    assert result.exit_code == 1
    assert "missing.yaml does not exist" in result.output
    assert "needs expires_at" in result.output
    assert "failed:" in result.output


def test_check_config_rejects_invalid(write_file):
    result = runner.invoke(app, ["check", "config", str(write_file("scout.yaml", "bogus: 1\n"))])
    assert result.exit_code == 1
    assert "bogus: unknown key" in result.output


def test_serve_refuses_invalid_config(write_file):
    result = runner.invoke(app, ["serve", "--config", str(write_file("scout.yaml", "server: {port: 0}\n"))])
    assert result.exit_code == 1
    assert "server.port" in result.output
