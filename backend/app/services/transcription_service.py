from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError
from app.models.enums import TranscriptionSource, TranscriptionStatus
from app.models.transcription import Transcription
from app.models.user import User
from app.repositories.transcription_repository import (
    TranscriptionFilters,
    TranscriptionRepository,
)
from app.schemas.transcription import (
    TranscriptionCreate,
    TranscriptionListParams,
    TranscriptionUpdate,
)

logger = logging.getLogger(__name__)


class TranscriptionNotFoundError(NotFoundError):
    code = "transcription_not_found"
    detail = "Transcription not found"


def default_title(now: datetime | None = None) -> str:
    moment = now or datetime.now(UTC)
    return f"Transcription - {moment:%d %b %Y, %H:%M} UTC"


class TranscriptionService:
    """Business logic for a user's transcriptions. All operations are owner-scoped:
    another user's transcription is indistinguishable from one that does not exist (404)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._repo = TranscriptionRepository(session)

    async def create_manual(self, user: User, data: TranscriptionCreate) -> Transcription:
        transcription = Transcription(
            user_id=user.id,
            title=data.title or default_title(),
            text=data.text,
            speech_mode=data.speech_mode,
            script_preference=data.script_preference,
            source=TranscriptionSource.MANUAL,
            status=TranscriptionStatus.COMPLETED,
        )
        await self._repo.add(transcription)
        await self._session.commit()
        logger.info("Transcription created", extra={"transcription_id": str(transcription.id)})
        return transcription

    async def list(
        self, user: User, params: TranscriptionListParams
    ) -> tuple[Sequence[Transcription], int]:
        filters = TranscriptionFilters(
            search=params.search,
            speech_mode=params.speech_mode,
            status=params.status,
            source=params.source,
        )
        return await self._repo.list_for_user(
            user.id, filters, offset=params.offset, limit=params.page_size
        )

    async def get(
        self, user: User, transcription_id: uuid.UUID, *, include_segments: bool = False
    ) -> Transcription:
        transcription = await self._repo.get_for_user(
            transcription_id, user.id, with_segments=include_segments
        )
        if transcription is None:
            raise TranscriptionNotFoundError()
        return transcription

    async def update(
        self, user: User, transcription_id: uuid.UUID, data: TranscriptionUpdate
    ) -> Transcription:
        transcription = await self.get(user, transcription_id)
        changed = False
        if data.title is not None and data.title != transcription.title:
            transcription.title = data.title
            changed = True
        if data.text is not None and data.text != transcription.text:
            transcription.text = data.text
            transcription.is_edited = True
            changed = True
        if changed:
            await self._session.commit()
            logger.info("Transcription updated", extra={"transcription_id": str(transcription.id)})
        return transcription

    async def delete(self, user: User, transcription_id: uuid.UUID) -> None:
        if not await self._repo.delete_for_user(transcription_id, user.id):
            raise TranscriptionNotFoundError()
        await self._session.commit()
        logger.info("Transcription deleted", extra={"transcription_id": str(transcription_id)})
