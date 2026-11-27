from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy import text as sql_text  # aliased: the model has a column named `text`
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.enums import (
    LANGUAGE_CODE_MAX_LENGTH,
    TITLE_MAX_LENGTH,
    ScriptPreference,
    SpeechMode,
    TranscriptionSource,
    TranscriptionStatus,
    sql_in,
)
from app.models.mixins import CreatedAtMixin, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.user import User


class Transcription(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One recording session (or manually created entry). `text` is the user-editable text."""

    __tablename__ = "transcriptions"
    __table_args__ = (
        CheckConstraint("char_length(btrim(title)) > 0", name="title_not_blank"),
        CheckConstraint(sql_in("speech_mode", SpeechMode), name="speech_mode_valid"),
        CheckConstraint(
            sql_in("script_preference", ScriptPreference), name="script_preference_valid"
        ),
        CheckConstraint(sql_in("source", TranscriptionSource), name="source_valid"),
        CheckConstraint(sql_in("status", TranscriptionStatus), name="status_valid"),
        CheckConstraint("duration_ms IS NULL OR duration_ms >= 0", name="duration_non_negative"),
        # Dashboard query: a user's transcriptions, newest first.
        Index("ix_transcriptions_user_id_created_at", "user_id", sql_text("created_at DESC")),
    )
    __mapper_args__ = {"eager_defaults": True}  # noqa: RUF012

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(TITLE_MAX_LENGTH), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    speech_mode: Mapped[str] = mapped_column(
        String(16), nullable=False, default=SpeechMode.AUTO, server_default=SpeechMode.AUTO
    )
    script_preference: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=ScriptPreference.AS_RECOGNISED,
        server_default=ScriptPreference.AS_RECOGNISED,
    )
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    detected_language: Mapped[str | None] = mapped_column(String(LANGUAGE_CODE_MAX_LENGTH))
    decode_strategy: Mapped[str | None] = mapped_column(String(32))
    model_name: Mapped[str | None] = mapped_column(String(64))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    is_edited: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=sql_text("false")
    )

    user: Mapped[User] = relationship(back_populates="transcriptions", lazy="raise")
    segments: Mapped[list[TranscriptionSegment]] = relationship(
        back_populates="transcription",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="TranscriptionSegment.seq",
        lazy="raise",
    )

    def __repr__(self) -> str:
        return f"<Transcription id={self.id} user_id={self.user_id}>"


class TranscriptionSegment(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """Raw, immutable machine output for one finalised utterance (written by the live
    pipeline in a later step). Times are milliseconds from the start of the recording."""

    __tablename__ = "transcription_segments"
    __table_args__ = (
        # Also serves as the index for "segments of a transcription, in order".
        UniqueConstraint("transcription_id", "seq"),
        CheckConstraint("seq >= 0", name="seq_non_negative"),
        CheckConstraint("start_ms >= 0", name="start_non_negative"),
        CheckConstraint("end_ms >= start_ms", name="end_after_start"),
    )
    __mapper_args__ = {"eager_defaults": True}  # noqa: RUF012

    transcription_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("transcriptions.id", ondelete="CASCADE"), nullable=False
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    start_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    end_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    raw_text: Mapped[str] = mapped_column(Text, nullable=False)
    language: Mapped[str | None] = mapped_column(String(LANGUAGE_CODE_MAX_LENGTH))
    avg_logprob: Mapped[float | None] = mapped_column(Float)

    transcription: Mapped[Transcription] = relationship(back_populates="segments", lazy="raise")

    def __repr__(self) -> str:
        return f"<TranscriptionSegment transcription_id={self.transcription_id} seq={self.seq}>"
