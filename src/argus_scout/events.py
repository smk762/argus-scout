"""Status events: the envelope every source (webhook, poller, analyzer) hands to scout.

Sources only emit the envelope. Scout owns scheduling, quiet hours,
repeat-until-ack and delivery (see :mod:`argus_scout.heartbeat`).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from argus_scout.severity import Severity, coerce_severity

SCHEMA_VERSION = 1
SUMMARY_MAX = 200


def parse_dt(value: Any) -> datetime | None:
    """An aware datetime from ISO text, a Unix timestamp or a datetime (naive is read as UTC)."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, int | float):
        return datetime.fromtimestamp(value, tz=UTC)
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


@dataclass
class StatusEvent:
    """One normalised status update."""

    event_id: str
    source: str
    severity: Severity
    generated_at: datetime
    summary: str
    body_md: str = ""
    speakable: bool = True
    requires_ack: bool = False
    quiet_exempt: bool = False
    expires_at: datetime | None = None
    link: str | None = None
    schema_version: int = SCHEMA_VERSION
    payload: dict[str, Any] = field(default_factory=dict)
    acked_at: datetime | None = None
    last_broadcast_at: datetime | None = None
    broadcast_count: int = 0
    ingested_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> StatusEvent:
        """Normalise a canonical envelope. Only ``source`` and a summary are required."""
        source = str(raw.get("source") or "").strip()
        if not source:
            raise ValueError("status event needs a source")
        summary = str(raw.get("summary") or raw.get("title") or "").strip()
        if not summary:
            raise ValueError("status event needs a summary")
        payload = raw.get("payload")
        return cls(
            event_id=str(raw.get("event_id") or f"{source}:{uuid4().hex[:12]}"),
            source=source,
            severity=coerce_severity(raw.get("severity")),
            generated_at=parse_dt(raw.get("generated_at")) or datetime.now(UTC),
            summary=summary[:SUMMARY_MAX],
            body_md=str(raw.get("body_md") or ""),
            speakable=bool(raw.get("speakable", True)),
            requires_ack=bool(raw.get("requires_ack", False)),
            quiet_exempt=bool(raw.get("quiet_exempt", False)),
            expires_at=parse_dt(raw.get("expires_at")),
            link=raw.get("link") or None,
            schema_version=int(raw.get("schema_version") or SCHEMA_VERSION),
            payload=payload if isinstance(payload, dict) else {},
        )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["severity"] = self.severity.value
        for key, val in data.items():
            if isinstance(val, datetime):
                data[key] = val.isoformat()
        return data

    @property
    def acked(self) -> bool:
        return self.acked_at is not None

    def expired(self, now: datetime | None = None) -> bool:
        if self.expires_at is None:
            return False
        return (now or datetime.now(UTC)) >= self.expires_at

    @property
    def fingerprint(self) -> str:
        """Content identity: the same source, severity and summary is the same news.

        Sources may re-emit an unchanged condition under a fresh event_id, so
        duplicate collapsing and change detection key on content, not id.
        """
        summary = " ".join(self.summary.lower().split())
        return f"{self.source}|{self.severity.value}|{summary}"


def group_duplicates(events: list[StatusEvent]) -> list[list[StatusEvent]]:
    """Group identical events (by fingerprint), keeping first-seen order."""
    groups: dict[str, list[StatusEvent]] = {}
    for event in events:
        groups.setdefault(event.fingerprint, []).append(event)
    return list(groups.values())
