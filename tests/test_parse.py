import json
import stat
from pathlib import Path

import pytest

from cusdev.parse import (
    ParseError,
    build_schema,
    build_system_prompt,
    claude_cli_backend,
    parse_transcript,
)
from cusdev.taxonomy import Taxonomy

TAXONOMY = Taxonomy.load(Path(__file__).parent.parent / "config" / "taxonomy.yaml")
TRANSCRIPT = "Индейку она съела сразу, вылизала миску. Утку понюхала и ушла."


class FakeBackend:
    def __init__(self, *payloads):
        self.payloads = list(payloads)
        self.calls = []

    def __call__(self, system, user, schema, model):
        self.calls.append({"system": system, "user": user, "schema": schema, "model": model})
        return self.payloads.pop(0)


GOOD = {
    "summary": "Индейка зашла, утка нет.",
    "speakers": "one",
    "items": [
        {
            "sku": "turkey",
            "verdict": "liked",
            "themes": ["palatability"],
            "quotes": [{"text": "вылизала миску", "theme": "palatability"}],
        }
    ],
}


def test_prompt_lists_taxonomy_keys_from_config():
    prompt = build_system_prompt(TAXONOMY)
    for key in [*TAXONOMY.skus, *TAXONOMY.themes, *TAXONOMY.verdicts, *TAXONOMY.formats]:
        assert f"- {key}:" in prompt


def test_tool_schema_pins_enums():
    item = build_schema(TAXONOMY)["$defs"]["FeedbackItem"]["properties"]
    assert item["sku"]["enum"] == [*TAXONOMY.skus, None]
    assert item["themes"]["items"]["enum"] == list(TAXONOMY.themes)


def test_parse_ok_passes_hints_and_schema():
    backend = FakeBackend(GOOD)
    parsed = parse_transcript(
        TRANSCRIPT,
        backend=backend,
        model="m",
        taxonomy=TAXONOMY,
        channel="voice",
        sku_hint="turkey",
    )
    assert parsed.result.items[0].sku == "turkey"
    call = backend.calls[0]
    assert call["schema"] == build_schema(TAXONOMY)
    assert "Канал: голосовое" in call["user"]
    assert "индейка" in call["user"]


def test_parse_drops_invented_quote():
    bad = {**GOOD, "items": [{**GOOD["items"][0], "quotes": [{"text": "кошка в восторге"}]}]}
    parsed = parse_transcript(TRANSCRIPT, backend=FakeBackend(bad), model="m", taxonomy=TAXONOMY)
    assert parsed.result.items[0].quotes == []
    assert any("не найдена" in w for w in parsed.warnings)


def test_parse_retries_once_on_invalid_then_succeeds():
    backend = FakeBackend({"items": "not-a-list"}, GOOD)
    parsed = parse_transcript(TRANSCRIPT, backend=backend, model="m", taxonomy=TAXONOMY)
    assert parsed.result.summary
    assert "не прошёл проверку" in backend.calls[1]["user"]


def test_parse_gives_up_after_second_failure():
    with pytest.raises(ParseError):
        parse_transcript(TRANSCRIPT, backend=FakeBackend(None, None), model="m", taxonomy=TAXONOMY)


def test_parse_rejects_empty_transcript():
    with pytest.raises(ParseError):
        parse_transcript("  ", backend=FakeBackend(), model="m", taxonomy=TAXONOMY)


def _fake_claude(tmp_path, monkeypatch, output: dict) -> None:
    """Подменяет бинарь claude скриптом, который печатает заданный JSON-результат."""
    script = tmp_path / "claude"
    script.write_text(f"#!/bin/sh\ncat > /dev/null\necho '{json.dumps(output)}'\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("CLAUDE_BIN", str(script))


def test_cli_backend_reads_structured_output(tmp_path, monkeypatch):
    _fake_claude(tmp_path, monkeypatch, {"is_error": False, "structured_output": {"a": 1}})
    assert claude_cli_backend("s", "u", {}, "m") == {"a": 1}


def test_cli_backend_surfaces_auth_error(tmp_path, monkeypatch):
    _fake_claude(tmp_path, monkeypatch, {"is_error": True, "result": "OAuth token has expired"})
    with pytest.raises(ParseError, match="expired"):
        claude_cli_backend("s", "u", {}, "m")
