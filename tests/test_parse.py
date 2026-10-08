from pathlib import Path
from types import SimpleNamespace

import pytest

from cusdev.parse import ParseError, build_system_prompt, build_tool, parse_transcript
from cusdev.taxonomy import Taxonomy

TAXONOMY = Taxonomy.load(Path(__file__).parent.parent / "config" / "taxonomy.yaml")
TRANSCRIPT = "Индейку она съела сразу, вылизала миску. Утку понюхала и ушла."


class FakeClient:
    def __init__(self, *payloads):
        self.payloads = list(payloads)
        self.calls = []
        self.messages = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        payload = self.payloads.pop(0)
        block = SimpleNamespace(type="tool_use", input=payload)
        return SimpleNamespace(content=[block] if payload is not None else [])


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
    item = build_tool(TAXONOMY)["input_schema"]["$defs"]["FeedbackItem"]["properties"]
    assert item["sku"]["enum"] == [*TAXONOMY.skus, None]
    assert item["themes"]["items"]["enum"] == list(TAXONOMY.themes)


def test_parse_ok_passes_hints_and_forces_tool():
    client = FakeClient(GOOD)
    parsed = parse_transcript(
        TRANSCRIPT, client=client, model="m", taxonomy=TAXONOMY, channel="voice", sku_hint="turkey"
    )
    assert parsed.result.items[0].sku == "turkey"
    call = client.calls[0]
    assert call["tool_choice"] == {"type": "tool", "name": "save_parse"}
    assert "Канал: голосовое" in call["messages"][0]["content"]
    assert "индейка" in call["messages"][0]["content"]


def test_parse_drops_invented_quote():
    bad = {**GOOD, "items": [{**GOOD["items"][0], "quotes": [{"text": "кошка в восторге"}]}]}
    parsed = parse_transcript(TRANSCRIPT, client=FakeClient(bad), model="m", taxonomy=TAXONOMY)
    assert parsed.result.items[0].quotes == []
    assert any("не найдена" in w for w in parsed.warnings)


def test_parse_retries_once_on_invalid_then_succeeds():
    client = FakeClient({"items": "not-a-list"}, GOOD)
    parsed = parse_transcript(TRANSCRIPT, client=client, model="m", taxonomy=TAXONOMY)
    assert parsed.result.summary
    assert "не прошёл проверку" in client.calls[1]["messages"][0]["content"]


def test_parse_gives_up_after_second_failure():
    with pytest.raises(ParseError):
        parse_transcript(TRANSCRIPT, client=FakeClient(None, None), model="m", taxonomy=TAXONOMY)


def test_parse_rejects_empty_transcript():
    with pytest.raises(ParseError):
        parse_transcript("  ", client=FakeClient(), model="m", taxonomy=TAXONOMY)
