"""HTTP: загрузка для команды, API агента (ARCHITECTURE.md, «Контракт агент ↔ сервер», Р12).

Запуск локально: .venv/bin/uvicorn cusdev.web.app:app --port 8210 --reload
"""

import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from cusdev import queue
from cusdev.config import Settings, get_settings
from cusdev.db import get_session
from cusdev.models import Conversation, FeedbackItemRow, Tester
from cusdev.schemas import ParseResult, SourceKind, Status
from cusdev.taxonomy import Taxonomy, get_taxonomy

COOKIE = "cusdev_key"
COOKIE_MAX_AGE = 90 * 24 * 3600

WEB = Path(__file__).parent

app = FastAPI(title="Kosh Kuse — голоса тестеров")
app.mount("/static", StaticFiles(directory=WEB / "static"), name="static")
templates = Jinja2Templates(directory=WEB / "templates")
# метка версии к /static/*: после деплоя браузеры команды не держат старые стили и скрипты
templates.env.globals["v"] = int(max(f.stat().st_mtime for f in (WEB / "static").iterdir()))

SessionDep = Annotated[Session, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
TaxonomyDep = Annotated[Taxonomy, Depends(get_taxonomy)]


# ───────────────────────── доступ ─────────────────────────


def require_team(request: Request, settings: SettingsDep) -> None:
    """Секретная ссылка: /?key=… ставит cookie. Пустой ACCESS_KEY — режим разработки."""
    if not settings.access_key:
        return
    if not secrets.compare_digest(request.cookies.get(COOKIE, ""), settings.access_key):
        raise HTTPException(401, "Откройте страницу по ссылке с ключом")


def require_agent(request: Request, settings: SettingsDep, session: SessionDep) -> None:
    if not settings.agent_token:
        raise HTTPException(503, "AGENT_TOKEN не задан на сервере")
    header = request.headers.get("authorization", "")
    if not secrets.compare_digest(header, f"Bearer {settings.agent_token}"):
        raise HTTPException(401, "неверный токен агента")
    queue.touch_agent(session)  # любой запрос агента — «на связи»


Team = Depends(require_team)
Agent = Depends(require_agent)


@app.get("/")
def index(request: Request, settings: SettingsDep, key: str | None = None) -> Response:
    if key is not None:
        if not settings.access_key or not secrets.compare_digest(key, settings.access_key):
            raise HTTPException(401, "Неверный ключ в ссылке")
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie(
            COOKIE, key, max_age=COOKIE_MAX_AGE, httponly=True, samesite="lax",
            secure=settings.app_base_url.startswith("https"),
        )  # fmt: skip
        return resp
    return _page(request, settings, "dashboard.html")


@app.get("/upload")
def upload_page(request: Request, settings: SettingsDep) -> Response:
    return _page(request, settings, "upload.html")


def _page(request: Request, settings: Settings, template: str) -> Response:
    try:
        require_team(request, settings)
    except HTTPException:
        return templates.TemplateResponse(request, "locked.html", status_code=401)
    return templates.TemplateResponse(request, template)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


# ───────────────────────── загрузка (команда) ─────────────────────────


class Added(BaseModel):
    filename: str | None
    result: str  # added | duplicate | error
    id: int | None = None
    message: str | None = None


def _intake(
    settings: Settings,
    tester: str,
    channel: str | None,
    sku_hint: str | None,
    talked_at: str | None,
    note: str | None,
) -> queue.Intake:
    """Дата из формы → UTC. SQLite часовой пояс не хранит, поэтому в базе всё в UTC."""
    when = None
    if talked_at:
        try:
            when = datetime.fromisoformat(talked_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise HTTPException(422, f"дата разговора не распознана: {talked_at}") from exc
        if when.tzinfo is None:  # без пояса — считаем, что это местное время команды
            when = when.replace(tzinfo=ZoneInfo(settings.timezone))
        when = when.astimezone(UTC)
    return queue.Intake(
        tester=tester, channel=channel, sku_hint=sku_hint, talked_at=when, note=note
    )


@app.post("/api/upload", dependencies=[Team])
async def upload(
    session: SessionDep,
    settings: SettingsDep,
    taxonomy: TaxonomyDep,
    files: Annotated[list[UploadFile], File()],
    tester: Annotated[str, Form()],
    channel: Annotated[str | None, Form()] = None,
    sku_hint: Annotated[str | None, Form()] = None,
    talked_at: Annotated[str | None, Form()] = None,
    note: Annotated[str | None, Form()] = None,
) -> list[Added]:
    intake = _intake(settings, tester, channel, sku_hint, talked_at, note)
    out = []
    for f in files:
        name = f.filename or "без имени"
        try:
            conv = queue.add_file(session, settings, taxonomy, intake, name, await f.read())
            out.append(Added(filename=name, result="added", id=conv.id))
        except queue.Duplicate as dup:
            out.append(Added(filename=name, result="duplicate", id=dup.existing.id,
                             message="этот файл уже есть"))  # fmt: skip
        except queue.QueueError as exc:
            out.append(Added(filename=name, result="error", message=str(exc)))
    return out


class TextIn(BaseModel):
    tester: str
    text: str
    channel: str | None = None
    sku_hint: str | None = None
    talked_at: str | None = None
    note: str | None = None


@app.post("/api/text", dependencies=[Team])
def upload_text(
    body: TextIn, session: SessionDep, settings: SettingsDep, taxonomy: TaxonomyDep
) -> Added:
    intake = _intake(settings, body.tester, body.channel, body.sku_hint, body.talked_at, body.note)
    try:
        conv = queue.add_text(session, taxonomy, intake, body.text)
    except queue.Duplicate as dup:
        return Added(
            filename=None, result="duplicate", id=dup.existing.id, message="этот текст уже есть"
        )
    except queue.QueueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return Added(filename=None, result="added", id=conv.id)


@app.get("/api/testers", dependencies=[Team])
def testers(session: SessionDep) -> list[str]:
    return list(session.scalars(select(Tester.name).order_by(Tester.name)))


class QueueItem(BaseModel):
    id: int
    tester: str
    kind: str
    filename: str | None
    duration_sec: int | None
    status: str
    overdue: bool
    error: str | None
    created_at: datetime
    drive_doc_url: str | None


class QueueOut(BaseModel):
    agent_online: bool
    agent_last_seen: datetime | None
    queued: int
    in_work: int
    failed: int
    items: list[QueueItem]


@app.get("/api/queue", dependencies=[Team])
def queue_view(session: SessionDep, settings: SettingsDep, limit: int = 50) -> QueueOut:
    status = queue.pipe_status(session, settings)
    convs = session.scalars(
        select(Conversation)
        .options(selectinload(Conversation.tester))
        .order_by(Conversation.created_at.desc(), Conversation.id.desc())
        .limit(min(limit, 200))
    )
    items = [
        QueueItem(
            id=c.id,
            tester=c.tester.name,
            kind=c.source_kind,
            filename=c.original_filename,
            duration_sec=c.duration_sec,
            status=c.status,
            overdue=queue.is_overdue(c, settings),
            error=c.error,
            created_at=queue.as_utc(c.created_at),
            drive_doc_url=c.drive_doc_url,
        )
        for c in convs
    ]
    return QueueOut(**status.__dict__, items=items)


@app.post("/api/conversations/{conv_id}/retry", dependencies=[Team], status_code=204)
def retry(conv_id: int, session: SessionDep) -> None:
    try:
        queue.retry(session, conv_id)
    except queue.QueueError as exc:
        raise HTTPException(409, str(exc)) from exc


# ───────────────────────── агент ─────────────────────────


class Job(BaseModel):
    id: int
    kind: str
    filename: str
    channel: str
    sku_hint: str | None


@app.post("/api/agent/heartbeat", dependencies=[Agent], status_code=204)
def heartbeat() -> None:
    return None


@app.post("/api/agent/next", dependencies=[Agent], response_model=None)
def agent_next(session: SessionDep, settings: SettingsDep) -> Job | Response:
    conv = queue.claim_next(session, settings)
    if conv is None:
        return Response(status_code=204)
    return Job(
        id=conv.id,
        kind=conv.source_kind,
        filename=queue.agent_filename(conv, settings),
        channel=conv.channel,
        sku_hint=conv.sku_hint,
    )


def _conv(session: Session, conv_id: int) -> Conversation:
    conv = session.get(Conversation, conv_id)
    if conv is None:
        raise HTTPException(404, "нет такой задачи")
    return conv


@app.get("/api/agent/jobs/{conv_id}/source", dependencies=[Agent], response_model=None)
def agent_source(conv_id: int, session: SessionDep, settings: SettingsDep) -> Response:
    conv = _conv(session, conv_id)
    if conv.source_kind == SourceKind.text:
        return PlainTextResponse(conv.transcript or "")
    path = settings.media_dir / (conv.source_path or "")
    if not conv.source_path or not path.is_file():
        raise HTTPException(410, "исходный файл пропал с сервера")
    return FileResponse(path, filename=queue.agent_filename(conv, settings))


def _queue_call(fn, *args) -> None:  # noqa: ANN001
    try:
        fn(*args)
    except queue.QueueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/api/agent/jobs/{conv_id}/transcript", dependencies=[Agent], status_code=204)
async def agent_transcript(conv_id: int, request: Request, session: SessionDep) -> None:
    text = (await request.body()).decode("utf-8", errors="replace")
    _queue_call(queue.save_transcript, session, conv_id, text)


class ResultIn(BaseModel):
    transcript: str
    result: ParseResult
    warnings: list[str] = []
    model: str


@app.post("/api/agent/jobs/{conv_id}/result", dependencies=[Agent], status_code=204)
def agent_result(conv_id: int, body: ResultIn, session: SessionDep, taxonomy: TaxonomyDep) -> None:
    _queue_call(
        queue.save_result, session, taxonomy, conv_id,
        body.transcript, body.result, body.warnings, body.model,
    )  # fmt: skip


class FailIn(BaseModel):
    reason: str


@app.post("/api/agent/jobs/{conv_id}/fail", dependencies=[Agent], status_code=204)
def agent_fail(conv_id: int, body: FailIn, session: SessionDep) -> None:
    _queue_call(queue.fail, session, conv_id, body.reason)


# ───────────────────────── дашборд (команда) ─────────────────────────


@app.get("/api/taxonomy", dependencies=[Team])
def taxonomy_view(taxonomy: TaxonomyDep) -> dict:
    return {
        "skus": taxonomy.skus,
        "formats": taxonomy.formats,
        "verdicts": taxonomy.verdicts,
        "themes": taxonomy.themes,
        "channels": taxonomy.channels,
        "default_channel": taxonomy.default_channel,
    }


def _local(dt: datetime, settings: Settings) -> str:
    return queue.as_utc(dt).astimezone(ZoneInfo(settings.timezone)).isoformat()


def _items(conv: Conversation) -> list[dict]:
    return [
        {
            "sku": it.sku,
            "format": it.format,
            "verdict": it.verdict,
            "themes": it.themes or [],
            "quotes": [{"text": q.text, "theme": q.theme} for q in it.quotes],
        }
        for it in conv.items
    ]


@app.get("/api/dashboard", dependencies=[Team])
def dashboard(session: SessionDep, settings: SettingsDep) -> dict:
    """Всё для трёх экранов одним ответом: на панели в десятки человек это килобайты."""
    convs = session.scalars(
        select(Conversation)
        .where(Conversation.status == Status.done)
        .options(
            selectinload(Conversation.tester),
            selectinload(Conversation.items).selectinload(FeedbackItemRow.quotes),
        )
        .order_by(Conversation.talked_at.desc())
    )
    return {
        "today": datetime.now(ZoneInfo(settings.timezone)).date().isoformat(),
        "testers": list(session.scalars(select(Tester.name).order_by(Tester.name))),
        "conversations": [
            {
                "id": c.id,
                "date": _local(c.talked_at, settings),
                "tester": c.tester.name,
                "channel": c.channel,
                "summary": c.summary or "",
                "doc": c.drive_doc_url,
                "items": _items(c),
            }
            for c in convs
        ],
    }


@app.get("/api/conversations/{conv_id}", dependencies=[Team])
def conversation(conv_id: int, session: SessionDep, settings: SettingsDep) -> dict:
    c = _conv(session, conv_id)
    return {
        "id": c.id,
        "date": _local(c.talked_at, settings),
        "tester": c.tester.name,
        "channel": c.channel,
        "kind": c.source_kind,
        "duration_sec": c.duration_sec,
        "note": c.note,
        "summary": c.summary or "",
        "speakers": c.speakers,
        "transcript": c.transcript or "",
        "doc": c.drive_doc_url,
        "items": _items(c),
    }


@app.get("/api/search", dependencies=[Team])
def search(q: str, session: SessionDep) -> list[int]:
    """Поиск по транскриптам и саммари. В Python, а не в SQL: lower() SQLite не знает кириллицу."""
    needle = q.strip().casefold().replace("ё", "е")
    if len(needle) < 2:
        return []
    rows = session.execute(
        select(Conversation.id, Conversation.transcript, Conversation.summary).where(
            Conversation.status == Status.done
        )
    )
    return [
        cid
        for cid, transcript, summary in rows
        if needle in f"{transcript or ''}\n{summary or ''}".casefold().replace("ё", "е")
    ]
