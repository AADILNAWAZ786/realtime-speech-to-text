"""Data access for transcriptions.

Ownership is enforced here: every method takes `user_id` and includes it in the WHERE
clause. There is intentionally no "get by id" without an owner.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import ColumnElement, delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.transcription import Transcription


@dataclass(frozen=True, slots=True)
class TranscriptionFilters:
    search: str | None = None
    speech_mode: str | None = None
    status: str | None = None
    source: str | None = None


def _escape_like(term: str) -> str:
    """Escape LIKE wildcards so user input is matched literally."""
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class TranscriptionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    @staticmethod
    def _conditions(user_id: uuid.UUID, filters: TranscriptionFilters) -> list[ColumnElement[bool]]:
        conditions: list[ColumnElement[bool]] = [Transcription.user_id == user_id]
        if filters.search:
            pattern = f"%{_escape_like(filters.search)}%"
            conditions.append(
                or_(
                    Transcription.title.ilike(pattern, escape="\\"),
                    Transcription.text.ilike(pattern, escape="\\"),
                )
            )
        if filters.speech_mode:
            conditions.append(Transcription.speech_mode == filters.speech_mode)
        if filters.status:
            conditions.append(Transcription.status == filters.status)
        if filters.source:
            conditions.append(Transcription.source == filters.source)
        return conditions

    async def list_for_user(
        self,
        user_id: uuid.UUID,
        filters: TranscriptionFilters,
        *,
        offset: int,
        limit: int,
    ) -> tuple[Sequence[Transcription], int]:
        conditions = self._conditions(user_id, filters)

        total_query = select(func.count()).select_from(Transcription).where(*conditions)
        total = (await self._session.execute(total_query)).scalar_one()

        items_query = (
            select(Transcription)
            .where(*conditions)
            .order_by(Transcription.created_at.desc(), Transcription.id.desc())
            .offset(offset)
            .limit(limit)
        )
        items = (await self._session.execute(items_query)).scalars().all()
        return items, total

    async def get_for_user(
        self,
        transcription_id: uuid.UUID,
        user_id: uuid.UUID,
        *,
        with_segments: bool = False,
    ) -> Transcription | None:
        query = select(Transcription).where(
            Transcription.id == transcription_id, Transcription.user_id == user_id
        )
        if with_segments:
            query = query.options(selectinload(Transcription.segments))
        return (await self._session.execute(query)).scalar_one_or_none()

    async def add(self, transcription: Transcription) -> Transcription:
        self._session.add(transcription)
        await self._session.flush()
        return transcription

    async def delete_for_user(self, transcription_id: uuid.UUID, user_id: uuid.UUID) -> bool:
        """Delete in one scoped statement; segments go via ON DELETE CASCADE."""
        result = await self._session.execute(
            delete(Transcription)
            .where(Transcription.id == transcription_id, Transcription.user_id == user_id)
            .returning(Transcription.id)
        )
        return result.scalar_one_or_none() is not None
