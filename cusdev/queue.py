"""Очередь разговоров: приём, аренда агентом, транскрипт, результат (ARCHITECTURE.md, Р1, Р2).

Вся логика статусов — здесь. Веб-слой (cusdev/web) только принимает HTTP и зовёт эти функции.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from cusdev import media
from cusdev.config import Settings
from cusdev.models import Conversation, FeedbackItemRow, QuoteRow, Setting, Tester
from cusdev.schemas import ParseResult, SourceKind, Status, sanitize
from cusdev.taxonomy import Taxonomy

AGENT_LAST_SEEN = "agent_last_seen"
ACTIVE = (Status.transcribing, Status.parsing)


class QueueError(ValueError):
    """Ошибка, которую можно показать человеку как есть."""


class Duplicate(QueueError):
    def __init__(self, existing: Conversation) -> None:
        super().__init__("этот файл уже есть")
        self.existing = existing


def now() -> datetime:
    return datetime.now(UTC)


def as_utc(dt: datetime | None) -> datetime | None:
    """SQLite отдаёт naive datetime; всё, что мы пишем, — UTC."""
    if dt is None or dt.tzinfo:
        return dt
    return dt.replace(tzinfo=UTC)


# ───────────────────────── тестеры ─────────────────────────


def get_or_create_tester(session: Session, name: str) -> Tester:
    name = " ".join(name.split())
    if not name:
        raise QueueError("не указан тестер")
    tester = session.scalar(select(Tester).where(Tester.name == name))
    if tester is None:
        tester = Tester(name=name)
        session.add(tester)
        session.flush()
    return tester


# ───────────────────────── приём ─────────────────────────


@dataclass
class Intake:
    """Поля формы загрузки (ТЗ, часть 1)."""

    tester: str
    channel: str | None = None
    sku_hint: str | None = None
    talked_at: datetime | None = None
    note: str | None = None


def _check_intake(intake: Intake, taxonomy: Taxonomy) -> tuple[str, str | None]:
    channel = intake.channel or taxonomy.default_channel
    if channel not in taxonomy.channels:
        raise QueueError(f"неизвестный канал: {channel}")
    sku_hint = intake.sku_hint or None
    if sku_hint and sku_hint not in taxonomy.skus:
        raise QueueError(f"неизвестное СКЮ: {sku_hint}")
    return channel, sku_hint


def _existing(session: Session, content_hash: str) -> Conversation | None:
    return session.scalar(select(Conversation).where(Conversation.content_hash == content_hash))


def _add(session: Session, conv: Conversation) -> Conversation:
    session.add(conv)
    try:
        session.commit()
    except IntegrityError as exc:  # гонка двух одинаковых загрузок
        session.rollback()
        existing = _existing(session, conv.content_hash)
        if existing:
            raise Duplicate(existing) from exc
        raise
    return conv


def add_file(
    session: Session,
    settings: Settings,
    taxonomy: Taxonomy,
    intake: Intake,
    filename: str,
    data: bytes,
) -> Conversation:
    kind = media.kind_by_ext(filename)
    if kind is None:
        raise QueueError(f"{filename}: такой формат не принимаем")
    if kind == SourceKind.text:
        return add_text(session, taxonomy, intake, data.decode("utf-8", errors="replace"), filename)
    if not data:
        raise QueueError(f"{filename}: пустой файл")
    content_hash = media.sha256_bytes(data)
    if existing := _existing(session, content_hash):
        raise Duplicate(existing)
    channel, sku_hint = _check_intake(intake, taxonomy)

    settings.media_dir.mkdir(parents=True, exist_ok=True)
    stem = settings.media_dir / content_hash[:16]
    original = stem.with_name(stem.name + "-orig" + Path(filename).suffix.lower())
    original.write_bytes(data)
    if kind == SourceKind.audio:
        source = media.normalize_audio(original, stem)
        duration = media.probe_duration(source)
    else:
        source, duration = original, None

    conv = Conversation(
        content_hash=content_hash,
        tester_id=get_or_create_tester(session, intake.tester).id,
        source_kind=kind,
        channel=channel,
        sku_hint=sku_hint,
        talked_at=intake.talked_at or now(),
        note=(intake.note or "").strip() or None,
        original_filename=filename,
        size_bytes=len(data),
        duration_sec=duration,
        source_path=source.name,
        status=Status.queued,
    )
    return _add(session, conv)


def add_text(
    session: Session,
    taxonomy: Taxonomy,
    intake: Intake,
    text: str,
    filename: str | None = None,
) -> Conversation:
    """Вставленная переписка: идёт мимо MacWhisper сразу на разбор (ТЗ, часть 1)."""
    if not text.strip():
        raise QueueError("пустой текст")
    content_hash = media.sha256_text(text)
    if existing := _existing(session, content_hash):
        raise Duplicate(existing)
    channel, sku_hint = _check_intake(
        Intake(**{**intake.__dict__, "channel": intake.channel or "chat"}), taxonomy
    )
    conv = Conversation(
        content_hash=content_hash,
        tester_id=get_or_create_tester(session, intake.tester).id,
        source_kind=SourceKind.text,
        channel=channel,
        sku_hint=sku_hint,
        talked_at=intake.talked_at or now(),
        note=(intake.note or "").strip() or None,
        original_filename=filename,
        size_bytes=len(text.encode()),
        transcript=text.strip(),
        status=Status.queued,
    )
    return _add(session, conv)


# ───────────────────────── агент ─────────────────────────


def touch_agent(session: Session) -> None:
    session.merge(Setting(key=AGENT_LAST_SEEN, value=now().isoformat()))
    session.commit()


def agent_last_seen(session: Session) -> datetime | None:
    row = session.get(Setting, AGENT_LAST_SEEN)
    return datetime.fromisoformat(row.value) if row else None


def release_stale(session: Session, settings: Settings) -> int:
    """Задачи, по которым агент молчит дольше аренды, возвращаются в очередь."""
    cutoff = now() - timedelta(minutes=settings.job_lease_minutes)
    res = session.execute(
        update(Conversation)
        .where(Conversation.status.in_(ACTIVE), Conversation.claimed_at < cutoff)
        .values(status=Status.queued, claimed_at=None)
    )
    session.commit()
    return res.rowcount


def claim_next(session: Session, settings: Settings) -> Conversation | None:
    """Старейшая задача из очереди уходит агенту в аренду (одна за раз, ТЗ, часть 2)."""
    release_stale(session, settings)
    conv = session.scalar(
        select(Conversation)
        .where(Conversation.status == Status.queued)
        .order_by(Conversation.created_at, Conversation.id)
        .limit(1)
    )
    if conv is None:
        return None
    first = Status.transcribing if conv.source_kind == SourceKind.audio else Status.parsing
    # условный UPDATE: если два агента пришли одновременно, задачу получит только один
    res = session.execute(
        update(Conversation)
        .where(Conversation.id == conv.id, Conversation.status == Status.queued)
        .values(status=first, claimed_at=now(), error=None)
    )
    session.commit()
    if res.rowcount != 1:
        return None
    session.refresh(conv)
    return conv


def agent_filename(conv: Conversation, settings: Settings) -> str:
    """`YYYY-MM-DD HH.MM Имя [abc123].m4a` (ARCHITECTURE.md, Р10)."""
    local = as_utc(conv.talked_at).astimezone(ZoneInfo(settings.timezone))
    stamp = local.strftime("%Y-%m-%d %H.%M")
    name = "".join(ch for ch in conv.tester.name if ch not in '/\\:*?"<>|').strip()
    ext = Path(conv.source_path).suffix if conv.source_path else ".txt"
    return f"{stamp} {name} [{conv.short_id}]{ext}"


def _active(session: Session, conv_id: int) -> Conversation:
    conv = session.get(Conversation, conv_id)
    if conv is None:
        raise QueueError("нет такой задачи")
    if conv.status not in ACTIVE:
        raise QueueError(f"задача не в работе (статус {conv.status})")
    return conv


def save_transcript(session: Session, conv_id: int, transcript: str) -> None:
    conv = _active(session, conv_id)
    if not transcript.strip():
        raise QueueError("пустой транскрипт")
    conv.transcript = transcript.strip()
    conv.status = Status.parsing
    conv.claimed_at = now()  # аренда продлевается на разбор
    session.commit()


def save_result(
    session: Session,
    taxonomy: Taxonomy,
    conv_id: int,
    transcript: str,
    result: ParseResult,
    warnings: list[str],
    model: str,
) -> Conversation:
    """Результат разбора. sanitize() прогоняется ещё раз: в базу ничего не идёт в обход."""
    conv = _active(session, conv_id)
    transcript = transcript.strip() or (conv.transcript or "")
    clean, server_warnings = sanitize(result, transcript, taxonomy)
    conv.items.clear()
    session.flush()
    for pos, item in enumerate(clean.items):
        row = FeedbackItemRow(
            position=pos,
            sku=item.sku,
            format=item.format,
            verdict=item.verdict,
            themes=item.themes,
            quotes=[QuoteRow(text=q.text, theme=q.theme) for q in item.quotes],
        )
        conv.items.append(row)
    conv.transcript = transcript
    conv.summary = clean.summary
    conv.speakers = clean.speakers
    conv.parse_model = model
    conv.parse_warnings = [*warnings, *server_warnings] or None
    conv.status = Status.done
    conv.claimed_at = None
    conv.error = None
    conv.drive_synced = False
    session.commit()
    return conv


def fail(session: Session, conv_id: int, reason: str) -> None:
    conv = _active(session, conv_id)
    conv.status = Status.failed
    conv.error = reason.strip()[:2000] or "без описания"
    conv.claimed_at = None
    session.commit()


def retry(session: Session, conv_id: int) -> None:
    conv = session.get(Conversation, conv_id)
    if conv is None or conv.status != Status.failed:
        raise QueueError("повторить можно только упавшую задачу")
    conv.status = Status.queued
    conv.error = None
    session.commit()


# ───────────────────────── статус для страницы ─────────────────────────


@dataclass
class PipeStatus:
    agent_last_seen: datetime | None
    agent_online: bool
    queued: int
    in_work: int
    failed: int


def pipe_status(session: Session, settings: Settings) -> PipeStatus:
    seen = agent_last_seen(session)
    online = seen is not None and now() - seen < timedelta(minutes=settings.agent_stale_minutes)
    counts = dict(
        session.execute(
            select(Conversation.status, func.count()).group_by(Conversation.status)
        ).all()
    )
    return PipeStatus(
        agent_last_seen=seen,
        agent_online=online,
        queued=counts.get(Status.queued, 0),
        in_work=sum(counts.get(s, 0) for s in ACTIVE),
        failed=counts.get(Status.failed, 0),
    )


def is_overdue(conv: Conversation, settings: Settings) -> bool:
    """Висит в очереди дольше QUEUE_WARN_HOURS — подсвечиваем (ТЗ, часть 2)."""
    if conv.status != Status.queued:
        return False
    return now() - as_utc(conv.created_at) > timedelta(hours=settings.queue_warn_hours)
