"""The source plugin interface.

A source turns something a producer says into :class:`~argus_scout.events.StatusEvent`\\ s. It is an
object with a ``kind`` (the ``kind:`` a ``sources:`` entry in ``scout.yaml`` names) and a
``config_model``: a pydantic model, normally a subclass of :class:`~argus_scout.config.SourceSettings`,
that validates one ``sources:`` entry of that kind. On top of that a source can do either or both of:

- **pull** (:class:`PollingSource`): ``async poll(cfg) -> list[StatusEvent]``, called by the scheduler.
  A polling config may carry ``interval`` (seconds) and ``resolve_missing`` (bool, default false for a plugin):
  when true, each poll is taken as the source's whole current state and the poll job resolves this source's
  open events the poll didn't return. Return a :class:`PollResult` with ``complete=False`` when part of the
  response couldn't be read, so nothing is resolved on a partial view.
- **push** (:class:`PushSource`): ``parse(cfg, body) -> list[StatusEvent]`` and
  ``authorize(cfg, authorization, ...)``, called by the HTTP route that receives a producer's POST.

Sources only produce events. They never ingest, announce or act; the :class:`~argus_scout.inbox.Inbox`
records what they return. A poller fetches only the status endpoint its config names, through
``argus_cortex.net``, and never connects to the hosts it reports on.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, MutableMapping
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel

from argus_scout.events import StatusEvent


class PollResult(list[StatusEvent]):
    """What a poll returned: the events (it *is* the list), and whether they are the source's whole current state.

    ``complete`` false means some of what the source reported couldn't be read (a skipped item), so an event
    missing from the list may still be live: the poll job then resolves nothing. A plain ``list`` from a plugin
    counts as complete; whether absence means *cleared* at all is the source config's ``resolve_missing``.
    """

    def __init__(self, events: Iterable[StatusEvent] = (), *, complete: bool = True) -> None:
        super().__init__(events)
        self.complete = complete


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


__all__ = ["PollResult", "PollingSource", "PushSource", "Source", "SourceAuthError", "SourceError"]
