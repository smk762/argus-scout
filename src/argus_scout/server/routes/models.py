"""Request bodies for the write routes. Unknown keys are refused, so a typo can't be silently ignored."""

from __future__ import annotations

import unicodedata
from typing import Annotated

from pydantic import AfterValidator, BaseModel, BeforeValidator, ConfigDict, Field, StringConstraints

BY_MAX = 100
REASON_MAX = 2000


def _no_controls(value: str) -> str:
    if any(unicodedata.category(ch).startswith("C") for ch in value):
        raise ValueError("must not contain control or format characters")
    return value


def _newlines(value: str) -> str:
    r"""``\r\n`` and a lone ``\r`` as ``\n``: a textarea or a Windows client sends CRLF."""
    return value.replace("\r\n", "\n").replace("\r", "\n")


def _no_controls_but_newlines(value: str) -> str:
    _no_controls(value.replace("\n", " ").replace("\t", " "))
    return value


By = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=BY_MAX), AfterValidator(_no_controls)
]
"""Who did it: a person's name or handle, recorded in the audit trail."""
Reason = Annotated[
    str,
    BeforeValidator(lambda v: _newlines(v) if isinstance(v, str) else v),
    StringConstraints(strip_whitespace=True, max_length=REASON_MAX),
    AfterValidator(_no_controls_but_newlines),
]


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AckBody(_Body):
    by: By


class DecisionBody(_Body):
    """An approve or reject: who decided, why, and the ``revision`` of the recommendation they viewed (from
    ``GET /api/recommendations/{id}``). Required: a decision is refused (409) if the content changed since."""

    by: By
    reason: Reason = ""
    revision: Annotated[int, Field(ge=0, strict=True)]


__all__ = ["AckBody", "DecisionBody"]
