"""Status sources as plugins: where status events come from.

See :mod:`argus_scout.sources.base` for the interface and :mod:`argus_scout.sources.registry` for
how kinds are found (the ``argus_scout.sources`` entry-point group) and bound to config entries.
"""

from argus_scout.sources.base import PollingSource, PollResult, PushSource, Source, SourceAuthError, SourceError
from argus_scout.sources.registry import (
    ENTRY_POINT_GROUP,
    BoundSource,
    Registry,
    Sources,
    build_sources,
    load_registry,
)

__all__ = [
    "ENTRY_POINT_GROUP",
    "BoundSource",
    "PollResult",
    "PollingSource",
    "PushSource",
    "Registry",
    "Source",
    "SourceAuthError",
    "SourceError",
    "Sources",
    "build_sources",
    "load_registry",
]
