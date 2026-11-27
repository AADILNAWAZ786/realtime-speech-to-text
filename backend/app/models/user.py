from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import Boolean, CheckConstraint, String, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.enums import DISPLAY_NAME_MAX_LENGTH, EMAIL_MAX_LENGTH
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.transcription import Transcription


class User(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "users"
    __table_args__ = (
        # Emails are normalised to lower case before storage; enforce it in the DB too
        # so the unique constraint is effectively case-insensitive.
        CheckConstraint("email = lower(email)", name="email_lowercase"),
    )
    # Fetch server-generated values (timestamps) via RETURNING, so attributes are never
    # left expired - an expired attribute would trigger an implicit (invalid) async load.
    __mapper_args__ = {"eager_defaults": True}  # noqa: RUF012

    email: Mapped[str] = mapped_column(String(EMAIL_MAX_LENGTH), nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(DISPLAY_NAME_MAX_LENGTH))
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )

    transcriptions: Mapped[list[Transcription]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,  # rely on ON DELETE CASCADE in the database
        lazy="raise",  # never lazy-load implicitly in async code
    )

    def __repr__(self) -> str:
        return f"<User id={self.id}>"
