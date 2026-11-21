"""Severity levels shared by status events, the heartbeat policy and recommendations."""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class Severity(StrEnum):
    INFO = "info"
    NOTICE = "notice"
    WARNING = "warning"
    CRITICAL = "critical"


_RANK = {sev.value: i for i, sev in enumerate(Severity)}


def coerce_severity(value: Any, default: Severity = Severity.INFO) -> Severity:
    """A Severity from an enum or a case-insensitive string; unknown values give ``default``."""
    if isinstance(value, Severity):
        return value
    text = str(value or "").strip().lower()
    return Severity(text) if text in _RANK else default


def rank(value: Any) -> int:
    """Order of a Severity or plain string, lowest first (unknown counts as info)."""
    return _RANK.get(str(getattr(value, "value", value) or "").strip().lower(), 0)
