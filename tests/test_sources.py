from __future__ import annotations

import functools
import json
import sys
import textwrap
from importlib.metadata import EntryPoint

import httpx
import pytest
from argus_cortex.net import Reply, afetch

from argus_scout.config import JsonPollSource, WebhookSource, parse_config
from argus_scout.inbox import InvalidEvent
from argus_scout.severity import Severity
from argus_scout.sources import (
    ENTRY_POINT_GROUP,
    PollingSource,
    PushSource,
    SourceAuthError,
    SourceError,
    build_sources,
    load_registry,
)
from argus_scout.sources import registry as registry_module
from argus_scout.sources.json_poll import allow_host, lookup, map_response, poll_once
from argus_scout.sources.webhook import MAX_EVENTS, check_token, parse_webhook

HOOK = WebhookSource(kind="webhook", name="ci", token_env="CI_TOKEN")

# --- webhook -------------------------------------------------------------------------------------------------


def test_webhook_single_envelope_from_bytes():
    body = json.dumps({"event_id": "run-7", "severity": "warning", "summary": "Build failed"}).encode()
    [event] = parse_webhook(HOOK, body)
    assert (event.source, event.event_id, event.severity) == ("ci", "run-7", Severity.WARNING)


def test_webhook_list_body():
    events = parse_webhook(HOOK, [{"summary": "one"}, {"source": "ci", "summary": "two"}])
    assert [e.summary for e in events] == ["one", "two"]
    assert events[0].event_id != events[1].event_id


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (b"{not json", "body is not JSON"),
        (b"\xff\xfe", "body is not"),
        (b'"hello"', "body must be an event object or a list of them, got str"),
        ({"source": "ci"}, "^summary: required"),
        ([{"summary": "ok"}, {"summary": "ok", "speakable": "perhaps"}], "^event 1: speakable"),
        ([{"summary": "ok"}, 7], "^event 1: expected an object, got int"),
        ({"source": "nas", "summary": "spoof"}, "does not match webhook source 'ci'"),
        ([{"summary": "x"}] * (MAX_EVENTS + 1), "at most 100 per request"),
    ],
)
def test_webhook_rejects_malformed_bodies(body, message):
    with pytest.raises(InvalidEvent, match=message):
        parse_webhook(HOOK, body)


def test_webhook_token():
    environ = {"CI_TOKEN": "s3cret", "SERVER_TOKEN": "server"}
    check_token(HOOK, "Bearer s3cret", environ=environ)
    check_token(HOOK, "bearer  s3cret ", environ=environ)
    for header in (None, "", "Bearer wrong", "s3cret", "Basic s3cret", "Bearer server"):
        with pytest.raises(SourceAuthError) as info:
            check_token(HOOK, header, fallback_env="SERVER_TOKEN", environ=environ)
        assert info.value.status == 401


def test_webhook_token_falls_back_to_server_token_and_fails_closed():
    shared = WebhookSource(kind="webhook", name="ci")
    check_token(shared, "Bearer server", fallback_env="SERVER_TOKEN", environ={"SERVER_TOKEN": "server"})
    with pytest.raises(SourceAuthError, match="no token configured") as info:
        check_token(shared, "Bearer anything", fallback_env="SERVER_TOKEN", environ={})
    assert info.value.status == 503
    with pytest.raises(SourceAuthError) as info:
        check_token(HOOK, "Bearer ", environ={"CI_TOKEN": "  "})
    assert info.value.status == 503


# --- json_poll -----------------------------------------------------------------------------------------------

POLL = JsonPollSource(
    kind="json_poll",
    name="alerts",
    url="http://status.lan:9093/api/alerts",
    items="data.alerts",
    fields={"summary": "labels.alertname", "severity": "labels.severity", "link": "links.0"},
    auth_env="ALERTS_AUTH",
)
ALERTS = {
    "data": {
        "alerts": [
            {"labels": {"alertname": "DiskFull", "severity": "critical"}, "links": ["http://x/1"]},
            {"labels": {"alertname": "UPSOnBattery", "severity": "warning"}},
            {"labels": {"severity": "warning"}},
        ]
    }
}


def test_lookup():
    assert lookup(ALERTS, "data.alerts.1.labels.alertname") == "UPSOnBattery"
    assert lookup(ALERTS, "data.alerts.-1.labels.severity") == "warning"
    assert lookup(ALERTS, "data.alerts.9") is lookup(ALERTS, "data.nope")


def test_map_response_with_stable_ids_and_skips():
    mapped = map_response(POLL, ALERTS)
    assert [(e.summary, e.severity, e.source) for e in mapped.events] == [
        ("DiskFull", Severity.CRITICAL, "alerts"),
        ("UPSOnBattery", Severity.WARNING, "alerts"),
    ]
    assert mapped.events[0].link == "http://x/1" and mapped.events[1].link is None
    assert mapped.skipped == ["item 2: summary: required"]
    again = map_response(POLL, ALERTS)
    assert [e.event_id for e in again.events] == [e.event_id for e in mapped.events]
    assert all(e.event_id.startswith("alerts:") for e in mapped.events)
    # Same headline, new severity: same id, so the inbox sees an escalation rather than a new event.
    worse = {"data": {"alerts": [{"labels": {"alertname": "UPSOnBattery", "severity": "critical"}}]}}
    assert map_response(POLL, worse).events[0].event_id == mapped.events[1].event_id


def test_map_response_single_item_without_fields():
    cfg = JsonPollSource(kind="json_poll", name="backups", url="http://backups:8080/status")
    [event] = map_response(cfg, {"summary": "Last backup 3h ago", "severity": "notice", "extra": 1}).events
    assert (event.event_id, event.source, event.severity) == ("backups", "backups", Severity.NOTICE)


def test_map_response_missing_items_path():
    with pytest.raises(SourceError, match="items path 'data.alerts' is not in the response"):
        map_response(POLL, {"data": {}})
    with pytest.raises(SourceError, match="is dict, not a list"):
        map_response(POLL, {"data": {"alerts": {}}})


def reply(body: object, status: int = 200) -> Reply:
    return Reply(status, {}, json.dumps(body).encode() if not isinstance(body, bytes) else body, POLL.url)


async def test_poll_once_uses_the_guarded_fetch_with_config_host_allowed():
    calls = []

    async def fetcher(url, **kwargs):
        calls.append((url, kwargs))
        return reply(ALERTS)

    events = await poll_once(POLL, environ={"ALERTS_AUTH": "Bearer abc"}, fetcher=fetcher)
    assert len(events) == 2
    [(url, kwargs)] = calls
    assert url == POLL.url
    assert kwargs["allow_hosts"] == ["status.lan:9093"]
    assert kwargs["headers"]["Authorization"] == "Bearer abc"
    assert kwargs["max_bytes"] <= 4 * 1024 * 1024


async def test_poll_once_failures_are_source_errors():
    async def fetcher(url, **kwargs):
        return reply(b"<html>", 200)

    with pytest.raises(SourceError, match="ALERTS_AUTH is not set"):
        await poll_once(POLL, environ={}, fetcher=fetcher)
    with pytest.raises(SourceError, match="not JSON"):
        await poll_once(POLL, environ={"ALERTS_AUTH": "x"}, fetcher=fetcher)

    async def failing(url, **kwargs):
        return reply({}, 503)

    with pytest.raises(SourceError, match="answered 503"):
        await poll_once(POLL, environ={"ALERTS_AUTH": "x"}, fetcher=failing)


async def test_poll_once_through_argus_cortex_net_reaches_a_lan_host():
    """The real afetch, with a stubbed resolver and transport: no network. The configured host resolves
    to a private address, which the allowlist admits; a redirect to another private host is refused."""
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/redirect":
            return httpx.Response(302, headers={"location": "http://other.lan/"})
        return httpx.Response(200, json=ALERTS)

    fetcher = functools.partial(afetch, transport=httpx.MockTransport(handler), resolve=lambda host, port: ["10.0.0.5"])
    events = await poll_once(POLL, environ={"ALERTS_AUTH": "Bearer abc"}, fetcher=fetcher)
    assert [e.summary for e in events] == ["DiskFull", "UPSOnBattery"]
    assert seen[0].headers["host"] == "status.lan:9093"
    assert seen[0].headers["authorization"] == "Bearer abc"

    redirecting = POLL.model_copy(update={"url": "http://status.lan:9093/redirect"})
    with pytest.raises(SourceError, match="other.lan resolves to a non-public address"):
        await poll_once(redirecting, environ={"ALERTS_AUTH": "x"}, fetcher=fetcher)


def test_allow_host():
    assert allow_host("https://status.example/x") == "status.example:443"
    assert allow_host("http://Status.lan/x") == "status.lan:80"
    assert allow_host("http://[fd00::1]:8080/") == "[fd00::1]:8080"


# --- registry ------------------------------------------------------------------------------------------------

PLUGIN_MODULE = """
from typing import Literal

from argus_scout.config import SourceSettings
from argus_scout.events import StatusEvent


class PigeonConfig(SourceSettings):
    kind: Literal["pigeon"]
    loft: str
    birds: int = 1


class Pigeon:
    kind = "pigeon"
    config_model = PigeonConfig

    async def poll(self, cfg):
        return [StatusEvent.from_dict({"source": cfg.name, "event_id": "coo", "summary": f"{cfg.birds} from {cfg.loft}"})]


class NotASource:
    pass
"""


@pytest.fixture
def plugin_module(tmp_path, monkeypatch):
    (tmp_path / "fake_scout_plugin.py").write_text(textwrap.dedent(PLUGIN_MODULE))
    monkeypatch.syspath_prepend(str(tmp_path))
    yield "fake_scout_plugin"
    sys.modules.pop("fake_scout_plugin", None)


def ep(name: str, value: str) -> EntryPoint:
    return EntryPoint(name=name, value=value, group=ENTRY_POINT_GROUP)


def test_builtins_are_registered_through_entry_points():
    registry = load_registry()
    assert {"webhook", "json_poll"} <= set(registry.kinds)
    assert registry.errors == {}
    assert isinstance(registry.kinds["webhook"], PushSource)
    assert isinstance(registry.kinds["json_poll"], PollingSource)


async def test_third_party_kind_from_an_entry_point(plugin_module, monkeypatch):
    monkeypatch.setattr(
        registry_module,
        "entry_points",
        lambda group: [ep("pigeon", f"{plugin_module}:Pigeon")] if group == ENTRY_POINT_GROUP else [],
    )
    config = parse_config(
        {
            "sources": [
                {"kind": "pigeon", "name": "roof", "loft": "north", "birds": 3},
                {"kind": "webhook", "name": "ci"},
            ]
        }
    )
    built = build_sources(config)
    assert built.errors == {}
    roof = built.get("roof")
    assert roof.kind == "pigeon" and roof.polls and not roof.pushes
    assert type(roof.config).__name__ == "PigeonConfig" and roof.config.birds == 3
    assert [s.name for s in built.polling] == ["roof"] and [s.name for s in built.push] == ["ci"]
    [event] = await roof.plugin.poll(roof.config)
    assert event.summary == "3 from north"


def test_bad_source_entries_do_not_stop_the_others(plugin_module):
    registry = load_registry(
        [
            ep("pigeon", f"{plugin_module}:Pigeon"),
            ep("broken", "no_such_module_anywhere:Thing"),
            ep("junk", f"{plugin_module}:NotASource"),
            ep("webhook", f"{plugin_module}:Pigeon"),  # can't take over a built-in kind
        ]
    )
    assert "failed to load" in registry.errors["broken"]
    assert "not a source plugin" in registry.errors["junk"]
    assert "built-in kind" in registry.errors["webhook"]
    assert registry.kinds["webhook"].kind == "webhook"
    config = parse_config(
        {
            "sources": [
                {"kind": "pigeon", "name": "typo", "loft": "n", "bird": 2},
                {"kind": "pigeon", "name": "missing"},
                {"kind": "broken", "name": "b"},
                {"kind": "carrier_pigeon", "name": "c"},
                {"kind": "pigeon", "name": "good", "loft": "south"},
                {"kind": "json_poll", "name": "poll", "url": "http://status.lan/"},
            ]
        }
    )
    built = build_sources(config, registry)
    assert [s.name for s in built.sources] == ["good", "poll"]
    assert built.errors["typo"] == "pigeon: bird: unknown key"
    assert built.errors["missing"] == "pigeon: loft: Field required"
    assert "did not load" in built.errors["b"]
    assert built.errors["c"].startswith("unknown source kind 'carrier_pigeon' (installed: json_poll, pigeon, webhook")
