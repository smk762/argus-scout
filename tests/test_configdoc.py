"""docs/config.md is generated from the config schema; it must not drift from it (regenerate with ``make docs``)."""

from __future__ import annotations

from pathlib import Path

from argus_scout import config as cfg
from argus_scout.channels.mask import MaskConfig
from argus_scout.channels.snapcast import SnapcastConfig
from argus_scout.configdoc import SECTIONS, render

DOC = Path(__file__).resolve().parent.parent / "docs" / "config.md"


def test_config_reference_matches_the_schema():
    assert DOC.read_text() == render(), "docs/config.md is stale: run `make docs` and commit it"


def test_every_section_and_key_is_documented():
    text = render()
    for name in cfg.ScoutConfig.model_fields:
        assert f"\n## {name}\n" in text, name
    documented = {s.model for s in SECTIONS}
    nested = {cfg.LLMConfig, cfg.LLMGpuConfig, cfg.EchoConfig, cfg.Briefing, cfg.WebhookSource, cfg.JsonPollSource}
    nested |= {cfg.TextChannel, cfg.WebhookChannel, SnapcastConfig, MaskConfig}
    assert nested <= documented
    for model in documented:
        for name, field in model.model_fields.items():
            assert f"| `{field.alias or name}` |" in text, f"{model.__name__}.{name}"
