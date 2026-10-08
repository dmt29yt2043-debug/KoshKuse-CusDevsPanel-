"""Контракт результата разбора одного разговора (ARCHITECTURE.md, Р6, Р7, Р11).

Модель возвращает ParseResult. Потом его прогоняют через sanitize(): ключи не из закрытых
списков превращаются в null, цитаты, которых нет в транскрипте дословно, выбрасываются.
Всё, что выброшено, попадает в warnings — это журнал разбора, а не ошибка.
"""

import re
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

from cusdev.taxonomy import Taxonomy

MAX_THEMES = 3
MAX_QUOTES = 3


class Status(StrEnum):
    queued = "queued"  # аудио ждёт агента
    transcribing = "transcribing"  # агент взял в аренду
    ocr = "ocr"  # скриншот ждёт распознавания текста
    parsing = "parsing"  # текст есть, ждёт разбора
    done = "done"
    failed = "failed"


class SourceKind(StrEnum):
    audio = "audio"
    text = "text"
    image = "image"


class Quote(BaseModel):
    text: str = Field(description="Дословная фраза тестера из транскрипта, без правок")
    theme: str | None = Field(default=None, description="Ключ темы или null")


class FeedbackItem(BaseModel):
    """Отзыв об одном СКЮ внутри разговора. sku = null — отзыв не про конкретный СКЮ."""

    sku: str | None = None
    format: str | None = None
    verdict: str | None = None
    # лимиты MAX_THEMES/MAX_QUOTES режет sanitize(), а не валидация: лишняя тема
    # не должна ронять весь разбор
    themes: list[str] = Field(default_factory=list)
    quotes: list[Quote] = Field(default_factory=list)


class ParseResult(BaseModel):
    summary: str = Field(description="Саммари разговора одним абзацем")
    speakers: Literal["one", "several", "unknown"] = "unknown"
    items: list[FeedbackItem] = Field(default_factory=list)


_QUOTES_RE = re.compile(r"[«»„“”\"'`]")
_PUNCT_EDGE_RE = re.compile(r"^[\s.,;:!?…—–-]+|[\s.,;:!?…—–-]+$")
_SPACE_RE = re.compile(r"\s+")


def normalize(text: str) -> str:
    """Нормализация для сравнения цитаты с транскриптом: регистр, ё, кавычки, пробелы."""
    text = text.lower().replace("ё", "е")
    text = _QUOTES_RE.sub("", text)
    text = _SPACE_RE.sub(" ", text)
    return _PUNCT_EDGE_RE.sub("", text)


def quote_in_transcript(quote: str, normalized_transcript: str) -> bool:
    q = normalize(quote)
    return bool(q) and q in normalized_transcript


def sanitize(
    result: ParseResult, transcript: str, taxonomy: Taxonomy
) -> tuple[ParseResult, list[str]]:
    """Приводит ответ модели к закрытым спискам и проверяет цитаты кодом. Не мутирует вход."""
    warnings: list[str] = []
    norm_transcript = normalize(transcript)

    def key_or_none(kind: str, key: str | None) -> str | None:
        if key is None or key in getattr(taxonomy, kind):
            return key
        warnings.append(f"{kind}: неизвестный ключ {key!r} → null")
        return None

    items = []
    for item in result.items:
        themes = []
        for theme in item.themes:
            checked = key_or_none("themes", theme)
            if checked and checked not in themes:
                themes.append(checked)
        if len(themes) > MAX_THEMES:
            warnings.append(f"тем больше {MAX_THEMES}, лишние отброшены: {themes[MAX_THEMES:]}")
            themes = themes[:MAX_THEMES]
        quotes = []
        for quote in item.quotes:
            if not quote_in_transcript(quote.text, norm_transcript):
                warnings.append(f"цитата не найдена в транскрипте, выброшена: {quote.text!r}")
                continue
            quotes.append(Quote(text=quote.text.strip(), theme=key_or_none("themes", quote.theme)))
        if len(quotes) > MAX_QUOTES:
            warnings.append(f"цитат больше {MAX_QUOTES}, лишние отброшены")
            quotes = quotes[:MAX_QUOTES]
        items.append(
            FeedbackItem(
                sku=key_or_none("skus", item.sku),
                format=key_or_none("formats", item.format),
                verdict=key_or_none("verdicts", item.verdict),
                themes=themes,
                quotes=quotes,
            )
        )
    return result.model_copy(update={"items": items}), warnings
