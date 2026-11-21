"""Read-only views: the latest report, the policy explainer and the plain-text wall.

- ``GET /api/reports/latest?kind=``: the newest report (of one kind, if given).
- ``GET /api/policy/explain``: the effective heartbeat policy, where each key came from (``default`` or
  ``file``), and for each open item the gate or rule that decides, right now, whether it is broadcast,
  reported and voiced (:func:`argus_scout.heartbeat.explain`, as the status cycle runs it).
- ``GET /wall``: the latest report and the open items as ``text/plain``, readable on a dumb terminal.
  Everything on it came from producers or the model, so control and format characters (terminal escapes,
  bidi overrides) are stripped before it is printed.
"""

from __future__ import annotations

import unicodedata
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse

from argus_scout.heartbeat import HeartbeatPolicy, explain
from argus_scout.inbox import EventGroup
from argus_scout.recommendations import Status
from argus_scout.server.deps import get_runtime
from argus_scout.store.base import Report

WALL_WIDTH = 100
WALL_RECOMMENDATIONS = 20


def _report_dict(report: Report) -> dict[str, Any]:
    return {
        "id": report.id,
        "kind": report.kind,
        "generated_at": report.generated_at.isoformat(),
        "text": report.text,
        "meta": report.meta,
    }


def load_policy(request: Request) -> HeartbeatPolicy:
    """The current policy; anything wrong with the file means the defaults, as in the status cycle."""
    policy_file = get_runtime(request).policy_file
    try:
        return policy_file.get()
    except Exception as exc:  # noqa: BLE001 — PolicyFile already falls back; this is the last line
        return HeartbeatPolicy(path=policy_file.path, file_error=str(exc))


def explain_state(request: Request) -> dict[str, Any]:
    """What the status cycle would decide if it ran now, for every open group."""
    runtime = get_runtime(request)
    now: datetime = runtime.inbox.now()
    local = now.astimezone(runtime.config.schedule.tz)
    policy = load_policy(request)
    resting = policy.rest_hours.contains(local)
    quiet = policy.quiet_hours.contains(local)
    items = []
    for group in runtime.inbox.open_grouped():
        decision = explain(group.latest, policy, now=now, quiet=quiet).to_dict()
        if resting:
            # The status cycle skips rest hours entirely: nothing goes out, whatever the gates say.
            decision |= {"broadcast": False, "report": False, "voice": False, "rule": f"rest_hours; {decision['rule']}"}
        items.append(
            {**decision, "summary": group.latest.summary, "count": group.count, "fingerprint": group.fingerprint}
        )
    return {
        "now": now.isoformat(),
        "timezone": runtime.config.schedule.timezone,
        "resting": resting,
        "quiet": quiet,
        "policy": policy.to_dict(),
        "items": items,
    }


# --- the wall ------------------------------------------------------------------------------------------------


def clean(text: str, *, multiline: bool = False) -> str:
    """``text`` with control and format characters removed (newlines kept when ``multiline``)."""
    keep = {"\n"} if multiline else set()
    out = "".join(
        ch if ch in keep or not unicodedata.category(ch).startswith("C") else ("    " if ch == "\t" else " ")
        for ch in text.replace("\r\n", "\n")
    )
    return out if multiline else " ".join(out.split())


def _clip(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


def _group_line(group: EventGroup) -> str:
    latest = group.latest
    count = f" (x{group.count})" if group.count > 1 else ""
    ack = " [ack needed]" if latest.requires_ack else ""
    line = f"  {latest.severity.value.upper():<8} {clean(latest.source)}: {clean(latest.summary)}{count}{ack}"
    return _clip(line, WALL_WIDTH)


def render_wall(request: Request) -> str:
    runtime = get_runtime(request)
    now = runtime.inbox.now().astimezone(runtime.config.schedule.tz)
    lines = [f"argus-scout  {now:%Y-%m-%d %H:%M %Z}", "=" * WALL_WIDTH, ""]

    report = runtime.store.latest_report()
    if report is None:
        lines += ["No report yet.", ""]
    else:
        when = report.generated_at.astimezone(runtime.config.schedule.tz)
        lines += [f"Latest report: {clean(report.kind)}, {when:%Y-%m-%d %H:%M}", "-" * WALL_WIDTH]
        lines += [clean(report.text, multiline=True).rstrip(), ""]

    groups = runtime.inbox.open_grouped()
    lines += [f"Open items: {len(groups)}", "-" * WALL_WIDTH]
    lines += [_group_line(g) for g in groups] or ["  Nothing open."]
    lines.append("")

    proposed = runtime.store.list_recommendations(Status.PROPOSED, limit=WALL_RECOMMENDATIONS + 1)
    lines += [
        f"Awaiting a decision: {len(proposed) if len(proposed) <= WALL_RECOMMENDATIONS else f'{WALL_RECOMMENDATIONS}+'}"
    ]
    lines += ["-" * WALL_WIDTH]
    lines += [
        # The id and revision first, so clipping a long title never hides what an approve or reject has to name.
        _clip(f"  {r.severity.value.upper():<8} [{r.id} rev {r.revision}] {clean(r.title) or '(untitled)'}", WALL_WIDTH)
        for r in proposed[:WALL_RECOMMENDATIONS]
    ] or ["  Nothing to decide."]
    return "\n".join(lines) + "\n"


def read_router() -> APIRouter:
    router = APIRouter(tags=["reads"])

    @router.get("/api/reports/latest")
    def latest_report(request: Request, kind: str | None = None) -> dict[str, Any]:
        report = get_runtime(request).store.latest_report(kind or None)
        if report is None:
            raise HTTPException(status_code=404, detail=f"no {kind} report yet" if kind else "no report yet")
        return _report_dict(report)

    @router.get("/api/policy/explain")
    def policy_explain(request: Request) -> dict[str, Any]:
        return explain_state(request)

    @router.get("/wall", response_class=PlainTextResponse)
    def wall(request: Request) -> PlainTextResponse:
        return PlainTextResponse(render_wall(request))

    return router


__all__ = ["clean", "explain_state", "read_router", "render_wall"]
