"""deploy/quickstart's demo overlay must work as shipped: valid configs, a simulator that publishes what the rules
and the dashboard query, a dashboard that asks only for metrics that exist, and pinned images."""

from __future__ import annotations

import importlib.util
import json
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from prometheus_client.parser import text_string_to_metric_families
from typer.testing import CliRunner

from argus_scout.cli import app
from argus_scout.config import load_config
from argus_scout.rules_file import load_signal_rules
from argus_scout.server.metrics import ScoutCollector, build_registry
from argus_scout.tariff import TariffFile, current_period, next_period_change

ROOT = Path(__file__).resolve().parent.parent
QUICKSTART = ROOT / "deploy" / "quickstart"
DEMO = QUICKSTART / "demo"
CONFIG = DEMO / "config"
DASHBOARD = DEMO / "grafana" / "dashboards" / "scout-demo.json"
VOICE_ENV = {"ARGUS_XTTS_URL": "http://xtts:8031", "ARGUS_MASK_URL": "http://mask:8109"}


class ComposeLoader(yaml.SafeLoader):
    """PyYAML plus compose's merge tags (``!override``, ``!reset``), read as the plain value they carry."""


for _tag in ("!override", "!reset"):
    ComposeLoader.add_constructor(
        _tag,
        lambda loader, node: (
            loader.construct_mapping(node)
            if isinstance(node, yaml.MappingNode)
            else loader.construct_sequence(node)
            if isinstance(node, yaml.SequenceNode)
            else loader.construct_scalar(node)
        ),
    )


def compose_file(name: str) -> dict:
    return yaml.load((QUICKSTART / name).read_text(), Loader=ComposeLoader)  # noqa: S506 — SafeLoader subclass


def load_simulator():
    spec = importlib.util.spec_from_file_location("demo_simulator", DEMO / "simulator" / "simulator.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def simulator():
    return load_simulator()


@pytest.fixture
def config_dir(tmp_path, simulator):
    """The demo's config directory as it is at runtime: with the tariff the simulator writes."""
    for path in CONFIG.glob("*.yaml"):
        (tmp_path / path.name).write_text(path.read_text())
    simulator.write_tariff(tmp_path / "tariff.yaml")
    return tmp_path


@pytest.mark.parametrize("name", ["scout.yaml", "scout.voice.yaml"])
def test_check_config_passes_with_the_simulators_tariff(config_dir, name, monkeypatch):
    for key, value in VOICE_ENV.items():
        monkeypatch.setenv(key, value)
    result = CliRunner().invoke(app, ["check", "config", str(config_dir / name)])
    assert result.exit_code == 0, result.output


def test_voice_config_is_the_demo_config_plus_speech(monkeypatch):
    for key, value in VOICE_ENV.items():
        monkeypatch.setenv(key, value)
    base = yaml.safe_load((CONFIG / "scout.yaml").read_text())
    voice = yaml.safe_load((CONFIG / "scout.voice.yaml").read_text())
    mask = [c for c in voice["channels"] if c["kind"] == "mask"]
    assert voice["channels"] == base["channels"] + mask and len(mask) == 1
    assert mask[0]["face"] == "stock-01" and "url" not in mask[0]  # ARGUS_MASK_URL, from compose.voice.yaml
    echo = voice["services"].pop("echo")
    assert echo["peer"] == "xtts" and echo.get("speaker") and "voice_clone_id" not in echo  # built-in, not a clone
    assert {k: v for k, v in voice.items() if k != "channels"} == {k: v for k, v in base.items() if k != "channels"}
    loaded = load_config(CONFIG / "scout.voice.yaml", load_env=False)
    assert str(loaded.services.echo.url).startswith("http://xtts:8031")
    assert base["services"]["llm"]["backend"] == "anthropic" and "key" not in str(base["services"]["llm"]).replace(
        "api_key_env", ""
    )


def test_demo_runs_no_local_model_or_voice_and_stores_no_key():
    """Claude over the API, echo and mask by URL: the overlays start no model or speech container, and the API key
    is passed through from the host environment, never written into a file."""
    demo, voice = compose_file("compose.demo.yaml")["services"], compose_file("compose.voice.yaml")["services"]
    assert demo["ollama"]["profiles"] == ["ollama"] and demo["ollama-pull"]["profiles"] == ["ollama"]
    assert set(voice) == {"scout", "first-brief"}, "voice uses the operator's echo and mask by URL"
    assert demo["scout"]["environment"]["ANTHROPIC_API_KEY"] == "${ANTHROPIC_API_KEY:-}"
    assert "ollama" not in demo["scout"]["depends_on"]
    assert "ANTHROPIC_API_KEY" not in (DEMO / "demo.sh").read_text().split("ensure_env() {")[1].split("}")[0]


def test_demo_config_is_loopback_writes_behind_a_token():
    config = load_config(CONFIG / "scout.yaml", load_env=False)
    assert config.server.token_env == "SCOUT_API_TOKEN" and not config.server.read_only
    compose = yaml.safe_load((QUICKSTART / "compose.yaml").read_text())
    assert compose["services"]["scout"]["ports"] == ["${SCOUT_PUBLISH:-127.0.0.1:8400}:8400"]
    for name in ("compose.demo.yaml", "compose.voice.yaml"):
        for service in compose_file(name)["services"].values():
            for port in service.get("ports", []):
                assert ":-127.0.0.1:" in port, f"{name}: {port} must default to loopback"


@pytest.mark.parametrize("hour,minute", [(12, 0), (0, 10), (23, 40)])
def test_simulator_tariff_puts_the_peak_now_at_any_hour(simulator, tmp_path, hour, minute):
    now = datetime(2026, 1, 1, hour, minute, tzinfo=UTC)
    path = tmp_path / "tariff.yaml"
    simulator.write_tariff(path, now)
    tariff = TariffFile(str(path)).get()
    assert tariff is not None
    local = now.replace(tzinfo=None)
    assert current_period(tariff, local).name == "peak"
    assert next_period_change(tariff, local) == ("off_peak", 40 * 60)


def test_simulator_scenarios(simulator):
    clock = iter(range(0, 10_000, 3600)).__next__
    house = simulator.House(None, clock=clock)
    with pytest.raises(KeyError):
        house.set("nope")
    house.set("peak-batch")
    samples = {
        (s.name, tuple(sorted(s.labels.items()))): s.value
        for family in text_string_to_metric_families(house.render())
        for s in family.samples
    }
    assert samples[("load_power_watts", (("load", "batch-render"), ("shiftable", "true")))] > 1500
    assert samples[("demo_scenario", (("name", "peak-batch"),))] == 1
    assert samples[("home_energy_kwh_total", ())] > 0
    temps = {name: spec["gpu"]["temp"] for name, spec in simulator.SCENARIOS.items()}
    assert temps["normal"] < 80 <= temps["gpu-hot"] < 90 <= temps["gpu-critical"]  # gpu-hot's two thresholds


PROMQL_GROUPING = re.compile(r"\b(?:by|without|on|ignoring|group_left|group_right)\s*\([^)]*\)")
PROMQL_KEYWORDS = {"and", "or", "unless", "offset", "bool"}


def metric_names(expr: str) -> set[str]:
    """The series names in a PromQL expression: what is left once label matchers, ranges, strings, grouping
    clauses, function names, keywords and numbers are taken out."""
    text = re.sub(r'"[^"]*"', "", expr)
    text = re.sub(r"\{[^}]*\}|\[[^\]]*\]", " ", text)
    text = PROMQL_GROUPING.sub(" ", text)
    text = re.sub(r"\b[A-Za-z_]\w*\s*\(", "(", text)
    return {w for w in re.findall(r"\b[A-Za-z_:][A-Za-z0-9_:]*\b", text) if w not in PROMQL_KEYWORDS}


def test_metric_name_parser_bites():
    assert metric_names('sum by (load) (load_power_watts{x="y"})') == {"load_power_watts"}
    assert metric_names("time() - scout_job_last_success_timestamp_seconds") == {
        "scout_job_last_success_timestamp_seconds"
    }
    assert metric_names("changes(demo_scenario[30s]) > 0 and demo_scenario == 1") == {"demo_scenario"}


def exposed_names(simulator) -> set[str]:
    """Every series name scout's /metrics, the simulator, node exporter and Prometheus can produce."""
    names = set(simulator.METRICS) | {"up"}
    for family in build_registry(ScoutCollector()).collect():
        names.add(family.name + ("_total" if family.type == "counter" else ""))
    return names


def dashboard_exprs() -> list[str]:
    dash = json.loads(DASHBOARD.read_text())
    exprs = [t["expr"] for p in dash["panels"] for t in p.get("targets", [])]
    exprs += [a["expr"] for a in dash["annotations"]["list"] if "expr" in a]
    return exprs


def test_dashboard_queries_only_metrics_that_exist(simulator):
    exprs = dashboard_exprs()
    assert len(exprs) > 10
    known = exposed_names(simulator)
    for expr in exprs:
        for name in metric_names(expr):
            assert name in known or name.startswith("node_"), f"{name!r} in {expr!r} is not exported by anything"
    used = set().union(*(metric_names(e) for e in exprs))
    assert {"scout_signal_rule_up", "scout_reports_total", "scout_recommendations", "scout_approvals_total"} <= used


def test_dashboard_check_bites(simulator):
    assert "scout_rule_latency_seconds" not in exposed_names(simulator)


def test_demo_rules_query_only_the_simulator_and_node_exporter(simulator):
    rules = load_signal_rules(CONFIG / "signals.yaml")
    assert not rules.file_error and not rules.errors
    known = set(simulator.METRICS) | {"up"}
    for rule in rules.rules:
        for name in metric_names(rule.query):
            assert name in known or name.startswith("node_"), f"{rule.name}: {name}"


def test_demo_approvals_meet_a_guardrail():
    """The scenario 3 story: a standing rule matches gpu-hot, whose escalation is critical (never auto-approved)."""
    approvals = yaml.safe_load((CONFIG / "approvals.yaml").read_text())["rules"]
    analyzers = {r["analyzer"] for r in approvals}
    rules = {r.name: r for r in load_signal_rules(CONFIG / "signals.yaml").rules}
    assert analyzers <= set(rules)
    assert any(step.severity.value == "critical" for step in rules["gpu-hot"].escalate)


def test_images_are_pinned():
    for name in ("compose.demo.yaml", "compose.voice.yaml"):
        for service, spec in compose_file(name)["services"].items():
            if "image" in spec:
                assert re.search(r":\d+\.\d+(\.\d+)?(-[a-z0-9.]+)?$", spec["image"]), (
                    f"{name}: {service} {spec['image']}"
                )


def test_demo_script_uses_routes_that_exist():
    script = (DEMO / "demo.sh").read_text()
    for route in ("/api/recommendations", "/api/reports", "/approve"):
        assert route in script
