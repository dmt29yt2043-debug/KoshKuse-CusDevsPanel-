"""Таблицы базы (ARCHITECTURE.md, «Данные»). SQLite, один писатель за раз."""

from datetime import UTC, datetime

from sqlalchemy import JSON, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from cusdev.schemas import Status


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class Tester(Base):
    __tablename__ = "testers"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    conversations: Mapped[list["Conversation"]] = relationship(back_populates="tester")


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[int] = mapped_column(primary_key=True)
    # sha256 исходного файла или текста: защита от дублей (ТЗ, часть 1)
    content_hash: Mapped[str] = mapped_column(String(64), unique=True)
    tester_id: Mapped[int] = mapped_column(ForeignKey("testers.id"))
    source_kind: Mapped[str] = mapped_column(String(10))  # SourceKind
    channel: Mapped[str] = mapped_column(String(20))  # ключ taxonomy.channels
    talked_at: Mapped[datetime]
    note: Mapped[str | None] = mapped_column(String(500))
    # подсказка из формы, ключ taxonomy.skus
    sku_hint: Mapped[str | None] = mapped_column(String(20))

    original_filename: Mapped[str | None] = mapped_column(String(255))
    # путь исходника для агента относительно media_dir (.m4a или картинка); у текста — None
    source_path: Mapped[str | None] = mapped_column(String(255))
    duration_sec: Mapped[int | None] = mapped_column(Integer)
    size_bytes: Mapped[int | None] = mapped_column(Integer)

    status: Mapped[str] = mapped_column(String(20), default=Status.queued, index=True)
    error: Mapped[str | None] = mapped_column(Text)
    claimed_at: Mapped[datetime | None]  # аренда агентом, см. JOB_LEASE_MINUTES

    transcript: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    speakers: Mapped[str | None] = mapped_column(String(10))  # one | several | unknown
    parse_model: Mapped[str | None] = mapped_column(String(60))
    parse_warnings: Mapped[list[str] | None] = mapped_column(JSON)

    drive_doc_url: Mapped[str | None] = mapped_column(String(500))
    drive_synced: Mapped[bool] = mapped_column(default=False)

    created_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)

    tester: Mapped[Tester] = relationship(back_populates="conversations")
    items: Mapped[list["FeedbackItemRow"]] = relationship(
        back_populates="conversation", cascade="all, delete-orphan"
    )

    @property
    def short_id(self) -> str:
        """Шесть символов для имени файла у агента (ARCHITECTURE.md, Р10)."""
        return self.content_hash[:6]


class FeedbackItemRow(Base):
    __tablename__ = "feedback_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    position: Mapped[int] = mapped_column(default=0)  # порядок в разговоре, для id «хэш12-№»
    sku: Mapped[str | None] = mapped_column(String(20), index=True)
    format: Mapped[str | None] = mapped_column(String(20))
    verdict: Mapped[str | None] = mapped_column(String(20), index=True)
    themes: Mapped[list[str]] = mapped_column(JSON, default=list)

    conversation: Mapped[Conversation] = relationship(back_populates="items")
    quotes: Mapped[list["QuoteRow"]] = relationship(
        back_populates="item", cascade="all, delete-orphan"
    )

    __table_args__ = (UniqueConstraint("conversation_id", "position"),)


class QuoteRow(Base):
    __tablename__ = "quotes"

    id: Mapped[int] = mapped_column(primary_key=True)
    feedback_item_id: Mapped[int] = mapped_column(
        ForeignKey("feedback_items.id", ondelete="CASCADE"), index=True
    )
    text: Mapped[str] = mapped_column(Text)
    theme: Mapped[str | None] = mapped_column(String(20))

    item: Mapped[FeedbackItemRow] = relationship(back_populates="quotes")


class Setting(Base):
    """Одиночные значения: agent_last_seen и т. п."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(60), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
