"""Initial schema: users, transcriptions, transcription_segments.

Revision ID: 0001
Revises:
Create Date: 2026-11-25

Requires PostgreSQL 13+ (gen_random_uuid() is built in from 13).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _timestamps(*, with_updated_at: bool = True) -> list[sa.Column[object]]:
    columns: list[sa.Column[object]] = [
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        )
    ]
    if with_updated_at:
        columns.append(
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("now()"),
                nullable=False,
            )
        )
    return columns


def _uuid_pk() -> sa.Column[object]:
    return sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False)


def upgrade() -> None:
    op.create_table(
        "users",
        _uuid_pk(),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("display_name", sa.String(length=100), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("email", name=op.f("uq_users_email")),
        sa.CheckConstraint("email = lower(email)", name=op.f("ck_users_email_lowercase")),
    )

    op.create_table(
        "transcriptions",
        _uuid_pk(),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("text", sa.Text(), server_default="", nullable=False),
        sa.Column("speech_mode", sa.String(length=16), server_default="auto", nullable=False),
        sa.Column(
            "script_preference",
            sa.String(length=16),
            server_default="as_recognised",
            nullable=False,
        ),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("detected_language", sa.String(length=8), nullable=True),
        sa.Column("decode_strategy", sa.String(length=32), nullable=True),
        sa.Column("model_name", sa.String(length=64), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("is_edited", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_transcriptions")),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_transcriptions_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "char_length(btrim(title)) > 0", name=op.f("ck_transcriptions_title_not_blank")
        ),
        sa.CheckConstraint(
            "speech_mode IN ('auto', 'en', 'hi', 'mixed')",
            name=op.f("ck_transcriptions_speech_mode_valid"),
        ),
        sa.CheckConstraint(
            "script_preference IN ('as_recognised', 'roman', 'devanagari')",
            name=op.f("ck_transcriptions_script_preference_valid"),
        ),
        sa.CheckConstraint(
            "source IN ('live', 'manual')", name=op.f("ck_transcriptions_source_valid")
        ),
        sa.CheckConstraint(
            "status IN ('recording', 'completed', 'interrupted')",
            name=op.f("ck_transcriptions_status_valid"),
        ),
        sa.CheckConstraint(
            "duration_ms IS NULL OR duration_ms >= 0",
            name=op.f("ck_transcriptions_duration_non_negative"),
        ),
    )
    op.create_index(
        "ix_transcriptions_user_id_created_at",
        "transcriptions",
        ["user_id", sa.text("created_at DESC")],
    )

    op.create_table(
        "transcription_segments",
        _uuid_pk(),
        sa.Column("transcription_id", sa.Uuid(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("start_ms", sa.Integer(), nullable=False),
        sa.Column("end_ms", sa.Integer(), nullable=False),
        sa.Column("raw_text", sa.Text(), nullable=False),
        sa.Column("language", sa.String(length=8), nullable=True),
        sa.Column("avg_logprob", sa.Float(), nullable=True),
        *_timestamps(with_updated_at=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_transcription_segments")),
        sa.ForeignKeyConstraint(
            ["transcription_id"],
            ["transcriptions.id"],
            name=op.f("fk_transcription_segments_transcription_id_transcriptions"),
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "transcription_id",
            "seq",
            name=op.f("uq_transcription_segments_transcription_id_seq"),
        ),
        sa.CheckConstraint("seq >= 0", name=op.f("ck_transcription_segments_seq_non_negative")),
        sa.CheckConstraint(
            "start_ms >= 0", name=op.f("ck_transcription_segments_start_non_negative")
        ),
        sa.CheckConstraint(
            "end_ms >= start_ms", name=op.f("ck_transcription_segments_end_after_start")
        ),
    )


def downgrade() -> None:
    op.drop_table("transcription_segments")
    op.drop_index("ix_transcriptions_user_id_created_at", table_name="transcriptions")
    op.drop_table("transcriptions")
    op.drop_table("users")
