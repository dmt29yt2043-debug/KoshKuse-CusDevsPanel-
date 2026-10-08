"""Разбор транскрипта одним вызовом Claude → ParseResult (ARCHITECTURE.md, Р2, Р11, Р14).

Claude вызывается через CLI Claude Code (`claude -p`) — по подписке Макса, без API-ключа.
Авторизация CLI: `claude setup-token` → CLAUDE_CODE_OAUTH_TOKEN в .env.

CLI для этапа 1:
    python -m cusdev.parse samples/*.txt                       # PARSE_MODEL из .env
    python -m cusdev.parse samples/*.txt -m claude-sonnet-5-5 -m claude-haiku-4-5
Печатает таблицу сравнения и пишет полный отчёт в samples/report.md (в git не попадает).
"""

import argparse
import json
import os
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from cusdev.config import get_settings
from cusdev.schemas import ParseResult, sanitize
from cusdev.taxonomy import Taxonomy, get_taxonomy

CLI_TIMEOUT_SEC = 600


class ParseError(RuntimeError):
    pass


# (system, user, json_schema, model) → dict от модели или None, если ответа нет
Backend = Callable[[str, str, dict[str, Any], str], dict[str, Any] | None]


def claude_cli_backend(
    system: str, user: str, schema: dict[str, Any], model: str
) -> dict[str, Any] | None:
    """Один вызов `claude -p` со строгой схемой. Без инструментов, MCP, CLAUDE.md и истории."""
    cmd = [
        os.environ.get("CLAUDE_BIN", "claude"),
        "-p",
        "--model", model,
        "--system-prompt", system,
        "--json-schema", json.dumps(schema, ensure_ascii=False),
        "--output-format", "json",
        "--tools", "",
        "--setting-sources", "",
        "--strict-mcp-config",
        "--no-session-persistence",
    ]  # fmt: skip
    env = dict(os.environ)
    token = get_settings().claude_code_oauth_token
    if token:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = token
    with tempfile.TemporaryDirectory() as cwd:  # чтобы не подтянулся CLAUDE.md репозитория
        proc = subprocess.run(
            cmd, input=user, capture_output=True, text=True,
            cwd=cwd, env=env, timeout=CLI_TIMEOUT_SEC,
        )  # fmt: skip
    try:
        out = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise ParseError(f"claude CLI: {proc.stderr.strip() or proc.stdout[:300]}") from exc
    if out.get("is_error"):
        raise ParseError(f"claude CLI: {out.get('result')}")
    structured = out.get("structured_output")
    if isinstance(structured, dict):
        return structured
    try:
        return json.loads(out.get("result") or "")
    except json.JSONDecodeError:
        return None


def _listing(items: dict[str, str]) -> str:
    return "\n".join(f"  - {key}: {label}" for key, label in items.items())


def build_system_prompt(taxonomy: Taxonomy) -> str:
    return f"""Ты разбираешь расшифровку разговора с тестером корма для кошек (бренд Kosh Kuse).
Расшифровка получена автоматически: пунктуация приблизительная, разметки спикеров нет,
реплики разных людей идут сплошным текстом. Ответ — строго JSON по заданной схеме.

ТРИ ЖЁСТКИХ ПРАВИЛА
1. Цитата — ДОСЛОВНО. Копируй фразу тестера символ в символ из расшифровки: без пересказа,
   без исправления оборотов, без склейки далёких кусков. Хорошая цитата — устойчивая живая
   фраза, 1–2 предложения. Лучше пропустить цитату, чем изменить в ней слово.
2. Не уверен — null. Вердикт, СКЮ или формат без опоры в тексте хуже пустого поля.
   Не выводи вердикт из тона разговора: нужно, чтобы тестер сам что-то оценил.
3. Только слова тестера. Если в записи говорят двое, реплики сотрудника Kosh Kuse
   (вопросы, пояснения, пересказ чужих слов) в цитаты не попадают, и вердикт — по словам тестера.
   Кто есть кто, определяй по смыслу: сотрудник спрашивает и объясняет про продукт, тестер
   рассказывает, как ел кот. Если по тексту не разобрать, чья фраза, — не цитируй её.
   Один голос на всю запись — speakers = "one"; заметно двое — "several"; не понять — "unknown".

ЧТО ВЕРНУТЬ
- summary: один абзац (3–5 предложений) по-русски: кто что пробовал и что в итоге сказал.
- items: по одному элементу на КАЖДЫЙ СКЮ, о котором тестер высказался. Если он сравнивает
  индейку с уткой — два элемента с разными вердиктами. Высказывания не про конкретный СКЮ
  (доставка, цена вообще) — один элемент с sku = null. Нет ни одного отзыва — items пустой.
- sku, format, verdict, themes, quotes[].theme — ТОЛЬКО ключи из списков ниже (или null).
  Ключ пиши точно, на латинице; русскую подпись в поле не подставляй.
- themes: до {3} самых важных тем отзыва. quotes: 1–{3} цитаты на элемент, лучшие.

СКЮ (sku):
{_listing(taxonomy.skus)}
Формат (format) — банка или сухой; это не СКЮ:
{_listing(taxonomy.formats)}
Вердикт (verdict):
{_listing(taxonomy.verdicts)}
Темы (themes):
{_listing(taxonomy.themes)}
"""


def build_user_message(
    transcript: str,
    taxonomy: Taxonomy,
    channel: str | None = None,
    sku_hint: str | None = None,
) -> str:
    context = []
    if channel:
        context.append(f"Канал: {taxonomy.label('channels', channel)}.")
    if sku_hint:
        context.append(
            f"Сотрудник отметил, что обсуждали: {taxonomy.label('skus', sku_hint)}. "
            "Это подсказка, а не факт: если в разговоре другое, верь разговору."
        )
    head = (" ".join(context) + "\n\n") if context else ""
    return f"{head}Расшифровка:\n<transcript>\n{transcript.strip()}\n</transcript>"


def build_schema(taxonomy: Taxonomy) -> dict[str, Any]:
    """JSON-схема ответа: ключи закрытых списков зашиты как enum, чтобы модель не выдумывала."""
    schema = ParseResult.model_json_schema()
    defs = schema["$defs"]

    def nullable_enum(values: dict[str, str]) -> dict[str, Any]:
        return {"enum": [*values, None]}

    item = defs["FeedbackItem"]["properties"]
    item["sku"] = nullable_enum(taxonomy.skus)
    item["format"] = nullable_enum(taxonomy.formats)
    item["verdict"] = nullable_enum(taxonomy.verdicts)
    item["themes"] = {"type": "array", "items": {"enum": list(taxonomy.themes)}, "maxItems": 3}
    defs["Quote"]["properties"]["theme"] = nullable_enum(taxonomy.themes)
    return schema


@dataclass
class Parsed:
    result: ParseResult
    warnings: list[str]
    model: str


def parse_transcript(
    transcript: str,
    *,
    model: str,
    backend: Backend = claude_cli_backend,
    taxonomy: Taxonomy | None = None,
    channel: str | None = None,
    sku_hint: str | None = None,
) -> Parsed:
    taxonomy = taxonomy or get_taxonomy()
    if not transcript.strip():
        raise ParseError("пустая расшифровка")
    schema = build_schema(taxonomy)
    system = build_system_prompt(taxonomy)
    last_error = ""
    for _ in range(2):  # один повтор, если модель вернула что-то невалидное
        message = build_user_message(transcript, taxonomy, channel, sku_hint)
        if last_error:
            message += f"\n\nПредыдущий ответ не прошёл проверку: {last_error}. Исправь."
        payload = backend(system, message, schema, model)
        if payload is None:
            last_error = "ответ не JSON"
            continue
        try:
            raw = ParseResult.model_validate(payload)
        except ValidationError as exc:
            last_error = str(exc).splitlines()[0]
            continue
        clean, warnings = sanitize(raw, transcript, taxonomy)
        return Parsed(result=clean, warnings=warnings, model=model)
    raise ParseError(f"модель {model} не вернула валидный результат: {last_error}")


# ───────────────────────── CLI: сравнение моделей на реальных записях ─────────────────────────


def _stats(parsed: Parsed) -> dict[str, int]:
    items = parsed.result.items
    dropped = sum(1 for w in parsed.warnings if w.startswith("цитата не найдена"))
    return {
        "items": len(items),
        "quotes": sum(len(i.quotes) for i in items),
        "dropped": dropped,
        "null_verdict": sum(1 for i in items if i.verdict is None),
        "null_sku": sum(1 for i in items if i.sku is None),
        "warnings": len(parsed.warnings),
    }


def _report_section(path: Path, parsed: Parsed, taxonomy: Taxonomy) -> str:
    lines = [f"### {path.name} — {parsed.model}", "", parsed.result.summary, ""]
    lines.append(f"Голосов: {parsed.result.speakers}")
    for n, item in enumerate(parsed.result.items, 1):
        themes = ", ".join(taxonomy.label("themes", t) for t in item.themes) or "—"
        lines.append(
            f"\n**Отзыв {n}:** СКЮ {taxonomy.label('skus', item.sku)} · "
            f"формат {taxonomy.label('formats', item.format)} · "
            f"вердикт {taxonomy.label('verdicts', item.verdict)} · темы: {themes}"
        )
        lines += [f"> {q.text}" for q in item.quotes]
    if parsed.warnings:
        lines += ["", "Предупреждения:", *[f"- {w}" for w in parsed.warnings]]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("files", nargs="+", type=Path, help="расшифровки .txt")
    ap.add_argument(
        "-m", "--model", action="append", help="можно несколько; по умолчанию PARSE_MODEL"
    )
    ap.add_argument("--channel", help="ключ канала (call / voice / chat)")
    ap.add_argument("--sku-hint", help="ключ СКЮ, как в форме загрузки")
    ap.add_argument("--report", type=Path, default=Path("samples/report.md"))
    args = ap.parse_args(argv)

    settings = get_settings()
    taxonomy = get_taxonomy()
    models = args.model or [settings.parse_model]

    sections: list[str] = []
    rows: list[str] = []
    header = (
        f"{'файл':<42}{'модель':<22}{'отз.':>5}{'цит.':>5}{'выбр.':>6}{'вердикт∅':>9}{'СКЮ∅':>5}"
    )
    for path in args.files:
        transcript = path.read_text(encoding="utf-8")
        for model in models:
            try:
                parsed = parse_transcript(
                    transcript,
                    model=model,
                    taxonomy=taxonomy,
                    channel=args.channel,
                    sku_hint=args.sku_hint,
                )
            except ParseError as exc:
                rows.append(f"{path.name[:40]:<42}{model:<22} ОШИБКА: {exc}")
                continue
            s = _stats(parsed)
            rows.append(
                f"{path.name[:40]:<42}{model:<22}{s['items']:>5}{s['quotes']:>5}"
                f"{s['dropped']:>6}{s['null_verdict']:>9}{s['null_sku']:>5}"
            )
            sections.append(_report_section(path, parsed, taxonomy))
    print(header, *rows, sep="\n")
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text("\n".join(sections), encoding="utf-8")
    print(f"\nПолный отчёт: {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
