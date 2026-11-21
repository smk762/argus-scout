from __future__ import annotations

from pathlib import Path

from argus_scout.persona import DEFAULT_PERSONA, RULES, PersonaFile, load_persona

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "persona.yaml"


def test_example_is_the_default():
    assert load_persona(EXAMPLE) == DEFAULT_PERSONA


def test_no_file_is_the_default():
    assert PersonaFile(None).get() is DEFAULT_PERSONA


def test_file_overrides_and_rules_stay_first(write_file):
    path = write_file("persona.yaml", "name: Wren\nsign_off: Over.\ndo: []\n")
    persona = PersonaFile(path).get()
    assert persona.name == "Wren" and persona.voice == DEFAULT_PERSONA.voice
    prompt = persona.system_prompt()
    assert prompt.startswith("Rules:")
    assert all(rule in prompt for rule in RULES)
    assert "End with: Over." in prompt and "Do:" not in prompt


def test_bad_file_degrades_to_default_and_recovers(write_file):
    path = write_file("persona.yaml", "name: Wren\nunknown_key: 1\n")
    file = PersonaFile(path)
    assert file.get() is DEFAULT_PERSONA
    assert file.error and "unknown_key" in file.error

    write_file("persona.yaml", "name: [unclosed\n")
    assert file.get() is DEFAULT_PERSONA and file.error

    write_file("persona.yaml", "- a list\n")
    assert file.get() is DEFAULT_PERSONA and "mapping" in file.error

    write_file("persona.yaml", "name: Wren\n")
    assert file.get().name == "Wren" and file.error is None


def test_missing_file_degrades_to_default(tmp_path):
    file = PersonaFile(tmp_path / "nope.yaml")
    assert file.get() is DEFAULT_PERSONA and file.error
