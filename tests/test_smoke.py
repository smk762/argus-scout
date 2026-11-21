from __future__ import annotations

import argus_scout


def test_version_is_exposed() -> None:
    assert isinstance(argus_scout.__version__, str)
    assert argus_scout.__version__
