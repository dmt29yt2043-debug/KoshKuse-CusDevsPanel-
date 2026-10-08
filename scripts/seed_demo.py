"""Наполняет ЛОКАЛЬНУЮ базу выдуманными разговорами из прототипа — чтобы смотреть страницы.

    .venv/bin/python scripts/seed_demo.py            # в DATA_DIR из .env (по умолчанию ./data)

Идёт настоящим путём: add_text → claim_next → save_result, так что sanitize() и статусы те же,
что в бою. Тестеры и цитаты выдуманы (scripts/demo_data.json). На проде не запускать.
"""

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.orm import sessionmaker

from cusdev import queue
from cusdev.config import get_settings
from cusdev.db import get_engine
from cusdev.models import Conversation
from cusdev.schemas import FeedbackItem, ParseResult, Quote
from cusdev.taxonomy import get_taxonomy

SILENT = ["Светлана О.", "Игорь Ж.", "Людмила К.", "Олег Б.", "Кира А."]  # в панели, но молчат


def main() -> int:
    settings = get_settings()
    if settings.app_base_url.startswith("https"):
        print("Похоже на прод (APP_BASE_URL на https) — демо-данные туда не льём.", file=sys.stderr)
        return 2
    taxonomy = get_taxonomy()
    data = json.loads((Path(__file__).parent / "demo_data.json").read_text(encoding="utf-8"))
    session = sessionmaker(get_engine(), expire_on_commit=False)()

    added = 0
    for date, tester, channel, summary, items in reversed(data):
        quotes = [q[0] for it in items for q in it[4]]
        transcript = f"{summary}\n\n" + "\n".join(f"— {q}." for q in quotes)
        talked = datetime.fromisoformat(f"{date}T12:00:00").replace(tzinfo=UTC)
        try:
            conv = queue.add_text(
                session, taxonomy,
                queue.Intake(tester=tester, channel=channel, talked_at=talked), transcript,
            )  # fmt: skip
        except queue.Duplicate:
            continue
        claimed = queue.claim_next(session, settings)
        assert claimed is not None and claimed.id == conv.id, "в очереди есть чужие задачи"
        result = ParseResult(
            summary=summary,
            speakers="one" if channel == "voice" else "several",
            items=[
                FeedbackItem(
                    sku=sku,
                    format=fmt,
                    verdict=verdict,
                    themes=themes,
                    quotes=[Quote(text=t, theme=th) for t, th in qs],
                )  # fmt: skip
                for sku, fmt, verdict, themes, qs in items
            ],
        )
        queue.save_result(session, taxonomy, conv.id, transcript, result, [], "demo")
        added += 1

    for name in SILENT:
        queue.get_or_create_tester(session, name)
    session.commit()
    total = session.query(Conversation).count()
    print(f"Добавлено разговоров: {added}, всего в базе: {total}. База: {settings.database_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
