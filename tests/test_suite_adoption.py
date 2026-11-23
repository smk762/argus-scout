"""argus-cortex 0.16: the suite's one ``/health`` body, the ``argus_build_info`` gauge, the suite-wide service URLs
and the compose labels the suite's directory is built from."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from api_helpers import make_app, scout_client
from argus_cortex.service import PEER_LABEL, ServiceHealth

import argus_scout
from argus_scout.channels import build_channels
from argus_scout.config import ConfigError, load_config, parse_config
from argus_scout.store import SqliteStore

ROOT = Path(__file__).resolve().parent.parent
QUICKSTART = ROOT / "deploy" / "quickstart"


@pytest.fixture
def store():
    with SqliteStore(":memory:") as s:
        yield s


# --- /health ---------------------------------------------------------------------------------------------------


def test_health_is_service_health_and_keeps_its_old_fields(store, tmp_path, monkeypatch):
    monkeypatch.setenv("ARGUS_PEER", "scout")
    with scout_client(make_app(store, tmp_path, read_only=True)) as client:
        reply = client.get("/health")
    assert reply.status_code == 200
    body = reply.json()
    # What it answered before: {"status": "ok", "version": ...}. Both keep their name and meaning.
    assert body["status"] == "ok" and body["version"] == argus_scout.__version__
    health = ServiceHealth.model_validate(body)
    assert (health.service, health.peer, health.read_only) == ("argus-scout", "scout", True)
    assert health.checks == {} and health.wire == {}, "scout probes nothing to answer /health"


def test_health_holds_nothing_from_the_config_or_the_store(store, tmp_path):
    with scout_client(make_app(store, tmp_path)) as client:
        body = client.get("/health").json()
    assert set(body) == {
        "service_version",
        "service",
        "version",
        "status",
        "started_at",
        "wire",
        "capabilities",
        "checks",
        "read_only",
        "build",
        "peer",
    }
    assert body["read_only"] is False and body["peer"] is None and body["build"] is None


def test_health_answers_without_a_token_any_host_and_under_read_auth(store, tmp_path):
    # Exactly as before: open to any Host, with no bearer token, read_auth or not, and before the app has started.
    with scout_client(make_app(store, tmp_path, read_auth=True), base_url="http://evil.example") as client:
        assert client.get("/health").status_code == 200
        assert client.get("/health?checks=0").json()["status"] == "ok"
        assert client.post("/health").status_code == 405
        assert client.get("/wall").status_code == 400  # the same Host is refused everywhere else


def test_metrics_carry_the_build_info_gauge(store, tmp_path):
    with scout_client(make_app(store, tmp_path)) as client:
        text = client.get("/metrics").text
    line = next(ln for ln in text.splitlines() if ln.startswith("argus_build_info{"))
    assert 'service="argus-scout"' in line and f'version="{argus_scout.__version__}"' in line
    assert line.endswith(" 1.0")


# --- suite-wide service URLs -----------------------------------------------------------------------------------


def echo_config(echo: dict):
    return parse_config({"services": {"echo": echo}}).services.echo


def test_echo_url_in_the_config_wins_over_the_suite_wide_name(monkeypatch):
    monkeypatch.setenv("ARGUS_XTTS_URL", "http://suite-xtts:8141")
    assert echo_config({"url": "http://own-echo:8000/"}).url == "http://own-echo:8000"
    assert echo_config({"url": "http://own-echo:8000", "peer": "xtts"}).url == "http://own-echo:8000"


@pytest.mark.parametrize(("peer", "variable"), [("xtts", "ARGUS_XTTS_URL"), ("tts", "ARGUS_TTS_URL")])
def test_echo_without_a_url_reads_its_peers_suite_wide_name(monkeypatch, peer, variable):
    monkeypatch.setenv("ARGUS_XTTS_URL", "http://suite-xtts:8141/")
    monkeypatch.setenv("ARGUS_TTS_URL", "http://suite-qwen:8142")
    expected = {"xtts": "http://suite-xtts:8141", "tts": "http://suite-qwen:8142"}[peer]
    assert echo_config({"peer": peer}).url == expected
    assert echo_config({"peer": peer, "url": " "}).url == expected, "blank is unset"
    monkeypatch.delenv(variable)
    with pytest.raises(ConfigError, match=f"services.echo: url is not set, and neither is {variable}"):
        echo_config({"peer": peer})


def test_echo_never_guesses_an_engine(monkeypatch):
    # Clones and speakers are per engine: with no url and no peer, a suite-wide URL is not picked for you.
    monkeypatch.setenv("ARGUS_XTTS_URL", "http://suite-xtts:8141")
    with pytest.raises(ConfigError, match="services.echo: url is not set"):
        echo_config({"language": "en"})
    with pytest.raises(ConfigError, match=r"services.echo.peer: Input should be 'xtts' or 'tts'"):
        echo_config({"peer": "miso"})


def test_a_malformed_suite_wide_url_is_refused_by_name_never_echoed(monkeypatch):
    monkeypatch.setenv("ARGUS_XTTS_URL", "user:hunter2@suite-xtts:8141")  # no scheme
    with pytest.raises(ConfigError) as caught:
        echo_config({"peer": "xtts"})
    assert "ARGUS_XTTS_URL" in str(caught.value) and "hunter2" not in str(caught.value)
    monkeypatch.setenv("ARGUS_XTTS_URL", "off")  # the suite's opt-out: no URL, so still a refused config
    with pytest.raises(ConfigError, match="neither is ARGUS_XTTS_URL"):
        echo_config({"peer": "xtts"})


def mask_channels(**entry):
    config = parse_config({"channels": [{"kind": "mask", "name": "face", "face": "ada", **entry}]})
    return build_channels(config)


def test_mask_url_in_the_config_wins_and_the_suite_wide_name_fills_in(monkeypatch):
    monkeypatch.setenv("ARGUS_MASK_URL", "http://suite-mask:8109/")
    own = mask_channels(url="http://own-mask:8000")
    assert not own.errors and own.channels[0].channel.config.url == "http://own-mask:8000"
    suite = mask_channels()
    assert not suite.errors and suite.channels[0].channel.config.url == "http://suite-mask:8109"


def test_mask_without_any_url_is_a_refused_channel_not_a_silent_one(monkeypatch):
    refused = mask_channels()
    assert not refused.channels and "neither is ARGUS_MASK_URL" in refused.errors["face"]
    monkeypatch.setenv("ARGUS_MASK_URL", "mask.lan:8109")
    bad = mask_channels().errors["face"]
    assert "ARGUS_MASK_URL" in bad and "mask.lan" not in bad


def test_a_suite_wide_url_in_the_env_file_beside_the_config_is_seen(tmp_path, monkeypatch):
    (tmp_path / "scout.yaml").write_text("services: {echo: {peer: tts}}\n")
    (tmp_path / ".env").write_text("ARGUS_TTS_URL=http://dotenv-qwen:8142\n")
    monkeypatch.setattr("os.environ", dict(__import__("os").environ))  # load_config writes into the environment
    assert load_config(tmp_path / "scout.yaml").services.echo.url == "http://dotenv-qwen:8142"


# --- compose ---------------------------------------------------------------------------------------------------


def test_quickstart_labels_scout_for_the_suite_directory():
    services = yaml.safe_load((QUICKSTART / "compose.yaml").read_text())["services"]
    assert services["scout"]["labels"] == {
        PEER_LABEL: "scout",
        "argus.service": "argus-scout",
        "argus.health": "/health",
        "argus.port": "8400",
    }
    assert services["scout"]["environment"]["ARGUS_PEER"] == "scout"
    labelled = [name for name, service in services.items() if PEER_LABEL in (service.get("labels") or {})]
    assert labelled == ["scout"], "only scout's own server is a peer: not the one-shot, not third-party containers"


def test_the_suite_file_mount_is_opt_in_and_read_only():
    main = (QUICKSTART / "compose.yaml").read_text()
    assert "/etc/argus/suite.env" not in main, "a host without a suite file must still start"
    override = yaml.safe_load((QUICKSTART / "compose.suite.yaml").read_text())
    for name in ("scout", "first-brief"):
        (mount,) = override["services"][name]["volumes"]
        assert mount["target"] == "/etc/argus/suite.env" and mount["read_only"] is True
        assert mount["bind"] == {"create_host_path": False}
