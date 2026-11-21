"""The source plugin interface.

A source turns something a producer says into :class:`~argus_scout.events.StatusEvent`\\ s. It is an
object with a ``kind`` (the ``kind:`` a ``sources:`` entry in ``scout.yaml`` names) and a
``config_model``: a pydantic model, normally a subclass of :class:`~argus_scout.config.SourceSettings`,
that validates one ``sources:`` entry of that kind. On top of that a source can do either or both of:

- **pull** (:class:`PollingSource`): ``async poll(cfg) -> list[StatusEvent]``, called by the scheduler.
  A polling config may carry ``interval`` (seconds).
- **push** (:class:`PushSource`): ``parse(cfg, body) -> list[StatusEvent]`` and
  ``authorize(cfg, authorization, ...)``, called by the HTTP route that receives a producer's POST.

Sources only produce events. They never ingest, announce or act; the :class:`~argus_scout.inbox.Inbox`
records what they return. A poller fetches only the status endpoint its config names, through
``argus_cortex.net``, and never connects to the hosts it reports on.
"""

from __future__ import annotations

from collections.abc import Mapping, MutableMapping
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel

from argus_scout.events import StatusEvent


class SourceError(Exception):
    """A source could not produce events (fetch refused or failed, response unusable)."""


class SourceAuthError(SourceError):
    """A push was refused. ``status`` is the HTTP status a route should answer with."""

    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.status = status


@runtime_checkable
class Source(Protocol):
    kind: str
    config_model: type[BaseModel]


@runtime_checkable
class PollingSource(Source, Protocol):
    async def poll(self, cfg: Any) -> list[StatusEvent]: ...


@runtime_checkable
class PushSource(Source, Protocol):
    def parse(self, cfg: Any, body: bytes | str | Mapping[str, Any] | list[Any]) -> list[StatusEvent]: ...

    def authorize(
        self,
        cfg: Any,
        authorization: str | None,
        *,
        fallback_env: str | None = None,
        environ: MutableMapping[str, str] | None = None,
    ) -> None: ...


__all__ = ["PollingSource", "PushSource", "Source", "SourceAuthError", "SourceError"]
