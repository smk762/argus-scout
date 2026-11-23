"""deploy/quickstart must work as shipped: a valid config, policy files identical to examples/, a compose file
whose services agree with that config, and pinned images."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlparse

import pytest
import yaml
from typer.testing import CliRunner

from argus_scout.cli import app
from argus_scout.config import TextChannel, load_config

ROOT = Path(__file__).resolve().parent.parent
QUICKSTART = ROOT / "deploy" / "quickstart"
CONFIG = QUICKSTART / "config" / "scout.yaml"
COPIED = ("signals", "heartbeat", "tariff", "approvals")


@pytest.fixture(scope="module")
def compose() -> dict:
    return yaml.safe_load((QUICKSTART / "compose.yaml").read_text())


@pytest.fixture(scope="module")
def config():
    return load_config(CONFIG, load_env=False)


@pytest.mark.parametrize("name", COPIED)
def test_policy_files_match_examples(name):
    # One source of truth: the quickstart ships copies, so they must not drift from examples/.
    copy, original = QUICKSTART / "config" / f"{name}.yaml", ROOT / "examples" / f"{name}.yaml"
    assert copy.read_text() == original.read_text(), f"cp examples/{name}.yaml deploy/quickstart/config/"


def test_check_config_passes():
    result = CliRunner().invoke(app, ["check", "config", str(CONFIG)])
    assert result.exit_code == 0, result.output
    assert "ok: 1 sources, 1 channels, 2 briefings" in result.output


def test_config_is_safe_and_delivers_text(config):
    assert config.server.read_only, "writes stay off until the operator sets a token"
    assert config.server.token_env == "SCOUT_API_TOKEN"
    text = [c for c in config.channels if isinstance(c, TextChannel)]
    assert text and text[0].enabled and text[0].dir == CONFIG.parent
    for name in COPIED:
        assert getattr(config.files, name) == CONFIG.parent / f"{name}.yaml"


def test_config_points_at_compose_services(config, compose):
    services = compose["services"]
    prometheus = urlparse(config.services.prometheus_url)
    assert prometheus.hostname in services and prometheus.port == 9090
    llm = urlparse(config.services.llm.base_url)
    assert llm.hostname in services and llm.port == 11434
    # The model the pull service fetches is the one scout asks for.
    assert services["ollama-pull"]["command"] == ["pull", config.services.llm.model]


def test_scout_service(compose):
    scout = compose["services"]["scout"]
    assert scout["build"]["context"] == "../.."
    assert (ROOT / scout["build"]["dockerfile"]).is_file()
    assert any(v.split(":")[:2] == ["./config", "/config"] for v in scout["volumes"])
    assert "/health" in " ".join(scout["healthcheck"]["test"])
    for port in scout["ports"]:
        assert port.startswith("${SCOUT_PUBLISH:-127.0.0.1:"), "published on loopback by default"


def test_images_are_pinned(compose):
    for name, service in compose["services"].items():
        if "build" in service or service["image"].startswith("${SCOUT_IMAGE"):
            continue
        tag = service["image"].rpartition(":")[2]
        assert re.fullmatch(r"v?\d+\.\d+\.\d+", tag), f"{name}: pin an exact version, not {service['image']}"


def test_prometheus_scrapes_what_the_rules_expect(config):
    scrape = yaml.safe_load((QUICKSTART / "prometheus.yml").read_text())
    jobs = {job["job_name"]: job for job in scrape["scrape_configs"]}
    # The example signal rules select node exporter as job="node".
    assert 'job="node"' in config.files.signals.read_text()
    assert jobs["node"]["static_configs"][0]["targets"] == ["node-exporter:9100"]
    assert jobs["scout"]["static_configs"][0]["targets"] == ["scout:8400"]


def test_quickstart_prometheus_labels_scout_as_a_suite_service(config):
    # The suite_status source reads series with an argus_peer label: out of the box that is scout itself.
    (suite,) = [s for s in config.sources if s.kind == "suite_status"]
    scrape = yaml.safe_load((QUICKSTART / "prometheus.yml").read_text())
    jobs = {job["job_name"]: job for job in scrape["scrape_configs"]}
    assert jobs["scout"]["static_configs"][0]["labels"]["argus_peer"] == "scout"
    assert suite.health_job not in jobs, "no probe job here: its own `up` would otherwise be read as a service"


def test_iris_override_scrapes_and_probes_the_directory(config, compose):
    """compose.iris.yaml: both suite jobs discover targets from argus-iris's /directory/sd, the probe job is the
    one the suite_status source leaves out of `up`, and the probe module tells ok from degraded."""
    (suite,) = [s for s in config.sources if s.kind == "suite_status"]
    scrape = yaml.safe_load((QUICKSTART / "prometheus.iris.yml").read_text())
    jobs = {job["job_name"]: job for job in scrape["scrape_configs"]}
    for name in ("argus", suite.health_job):
        (sd,) = jobs[name]["http_sd_configs"]
        assert sd["url"].endswith("/directory/sd")
    probe = jobs[suite.health_job]
    assert probe["metrics_path"] == "/probe"
    relabel = probe["relabel_configs"]
    assert relabel[0]["source_labels"] == ["__scheme__", "__address__", "argus_health_path"]
    assert relabel[0]["target_label"] == "__param_target"
    assert {r["target_label"]: r.get("replacement") for r in relabel[2:]} == {
        "__address__": "blackbox:9115",
        "__scheme__": "http",
    }
    assert "labels" not in jobs["scout"]["static_configs"][0], "iris lists scout: don't list it twice"
    assert jobs["node"] and jobs["prometheus"], "the base jobs stay: the example signal rules need them"

    blackbox = yaml.safe_load((QUICKSTART / "blackbox.yml").read_text())
    (module,) = probe["params"]["module"]
    http = blackbox["modules"][module]["http"]
    assert http["valid_status_codes"] == [200] and http["follow_redirects"] is False
    (pattern,) = http["fail_if_body_not_matches_regexp"]
    assert re.search(pattern, '{"service":"argus-lens","status":"ok","checks":{}}')
    assert re.search(pattern, '{"status": "ok"}')
    assert not re.search(pattern, '{"status":"degraded","checks":{"store":{"ok":false}}}')

    override = yaml.safe_load((QUICKSTART / "compose.iris.yaml").read_text())
    base_mount = next(v for v in compose["services"]["prometheus"]["volumes"] if "prometheus.yml" in v)
    (mount,) = override["services"]["prometheus"]["volumes"]
    assert mount.split(":")[1] == base_mount.split(":")[1] == "/etc/prometheus/prometheus.yml"
    image = override["services"]["blackbox"]["image"]
    assert re.fullmatch(r"v?\d+\.\d+\.\d+", image.rpartition(":")[2]), "pin an exact version"
