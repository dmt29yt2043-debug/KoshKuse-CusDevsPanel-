from pathlib import Path

import pytest
from sqlalchemy import create_engine

from cusdev.models import Base
from cusdev.schemas import FeedbackItem, ParseResult, Quote, normalize, sanitize
from cusdev.taxonomy import Taxonomy

TAXONOMY = Taxonomy.load(Path(__file__).parent.parent / "config" / "taxonomy.yaml")

TRANSCRIPT = """Ну, индейку она съела сразу, прямо вылизала миску.
А вот утку понюхала и ушла. Запах, наверное, ей не зашёл."""


def test_taxonomy_loads_and_labels():
    assert TAXONOMY.label("skus", "turkey") == "индейка"
    assert TAXONOMY.label("skus", None) == "—"
    assert TAXONOMY.label("skus", "unknown-key") == "unknown-key"
    assert TAXONOMY.default_channel == "call"


def test_taxonomy_rejects_cyrillic_keys(tmp_path):
    bad = tmp_path / "t.yaml"
    bad.write_text(
        "skus: {индейка: индейка}\nformats: {wet: банка}\nverdicts: {liked: зашло}\n"
        "themes: {taste: вкус}\nchannels: {call: звонок}\ndefault_channel: call\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="латиницей"):
        Taxonomy.load(bad)


def test_normalize_ignores_case_yo_quotes_and_spacing():
    assert normalize("  «Запах,   НАВЕРНОЕ, ей не зашёл»! ") == "запах, наверное, ей не зашел"


def test_sanitize_keeps_verbatim_drops_invented_and_unknown_keys():
    raw = ParseResult(
        summary="Индейка зашла, утка — нет.",
        speakers="one",
        items=[
            FeedbackItem(
                sku="turkey",
                verdict="liked",
                themes=["palatability", "vibes"],
                quotes=[
                    Quote(text="прямо вылизала миску", theme="palatability"),
                    Quote(text="кошка в восторге от индейки"),  # пересказ, не дословно
                ],
            ),
            FeedbackItem(
                sku="rabbit",  # нет в списке
                verdict="disliked",
                themes=["smell", "taste", "texture", "price"],
                quotes=[Quote(text="Запах, наверное, ей не зашел", theme="smell")],
            ),
        ],
    )

    clean, warnings = sanitize(raw, TRANSCRIPT, TAXONOMY)

    turkey, other = clean.items
    assert turkey.themes == ["palatability"]
    assert [q.text for q in turkey.quotes] == ["прямо вылизала миску"]
    assert other.sku is None
    assert other.themes == ["smell", "taste", "texture"]
    assert [q.text for q in other.quotes] == ["Запах, наверное, ей не зашел"]
    assert len(warnings) == 4  # vibes, выдуманная цитата, rabbit, четвёртая тема
    assert raw.items[0].themes == ["palatability", "vibes"]  # вход не мутирован


def test_models_create_on_sqlite():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    assert {"testers", "conversations", "feedback_items", "quotes", "settings"} <= set(
        Base.metadata.tables
    )
