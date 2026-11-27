"""Database persistence for live recordings.

- Every operation uses its own short-lived AsyncSession (nothing is held open for the
  length of a recording).
- `append_final` stores one final segment AND appends its displayed text to
  `transcriptions.text` in a single transaction.
- Exactly once: the UNIQUE (transcription_id, seq) constraint rejects a duplicate insert,
  which rolls back the whole transaction, so the text is never appended twice either. A
  retry after an ambiguous failure (committed, but the acknowledgement was lost) is
  therefore safe and reported as already saved.
- Partial transcripts never reach this module.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy import case, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.enums import ScriptPreference, SpeechMode, TranscriptionSource, TranscriptionStatus
from app.models.transcription import Transcription, TranscriptionSegment
from app.realtime.events import FinalTranscript
from app.speech.script import render

logger = logging.getLogger(__name__)

_DUPLICATE_SEGMENT = "uq_transcription_segments_transcription_id_seq"


class SaveOutcome(StrEnum):
    SAVED = "saved"
    ALREADY_SAVED = "already_saved"


@dataclass(frozen=True, slots=True)
class DisplayedFinal:
    """A final as stored: raw model text plus the text rendered in the user's script."""

    final: FinalTranscript
    displayed_text: str


def displayed_text(final: FinalTranscript, preference: ScriptPreference) -> str:
    """Script conversion is applied here, separately from the raw text that is stored."""
    return render(final.text, preference).text


class RecordingStore:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        retry_delays: Sequence[float] = (0.1, 0.5, 1.0),
    ) -> None:
        self._sessions = session_factory
        self._retry_delays = tuple(retry_delays)

    # ---------------------------------------------------------------- lifecycle

    async def create_recording(
        self,
        *,
        user_id: uuid.UUID,
        title: str,
        speech_mode: SpeechMode,
        script_preference: ScriptPreference,
        model_name: str,
    ) -> uuid.UUID:
        async with self._sessions() as session, session.begin():
            transcription = Transcription(
                user_id=user_id,
                title=title,
                text="",
                speech_mode=speech_mode,
                script_preference=script_preference,
                source=TranscriptionSource.LIVE,
                status=TranscriptionStatus.RECORDING,
                model_name=model_name[:64],
                is_edited=False,
            )
            session.add(transcription)
            await session.flush()
            return transcription.id

    async def finish_recording(
        self,
        transcription_id: uuid.UUID,
        *,
        status: TranscriptionStatus,
        duration_ms: int,
        language: str | None,
    ) -> None:
        async def operation() -> None:
            async with self._sessions() as session, session.begin():
                await session.execute(
                    update(Transcription)
                    .where(Transcription.id == transcription_id)
                    .values(
                        status=status,
                        duration_ms=max(duration_ms, 0),
                        detected_language=func.coalesce(Transcription.detected_language, language),
                    )
                )

        await self._with_retries(operation, what="finish recording")

    async def mark_stale_recordings_interrupted(self) -> int:
        """After a crash/restart no recording can still be live: close leftovers."""
        async with self._sessions() as session, session.begin():
            result = await session.execute(
                update(Transcription)
                .where(Transcription.status == TranscriptionStatus.RECORDING)
                .values(status=TranscriptionStatus.INTERRUPTED)
                .returning(Transcription.id)
            )
            count = len(result.all())
        if count:
            logger.warning("Marked %d stale recording(s) as interrupted", count)
        return count

    # ---------------------------------------------------------------- finals

    async def append_final(self, transcription_id: uuid.UUID, item: DisplayedFinal) -> SaveOutcome:
        """Persist one final segment and append its text, atomically and exactly once."""
        final = item.final

        async def operation() -> SaveOutcome:
            try:
                async with self._sessions() as session, session.begin():
                    session.add(
                        TranscriptionSegment(
                            transcription_id=transcription_id,
                            seq=final.seq,
                            start_ms=final.start_ms,
                            end_ms=final.end_ms,
                            raw_text=final.text,  # raw model output, never script-converted
                            language=final.language[:8] if final.language else None,
                            avg_logprob=final.avg_logprob,
                        )
                    )
                    await session.flush()  # surfaces a duplicate seq before the text update
                    shown = item.displayed_text
                    await session.execute(
                        update(Transcription)
                        .where(Transcription.id == transcription_id)
                        .values(
                            text=case(
                                (Transcription.text == "", shown),
                                else_=Transcription.text + " " + shown,
                            ),
                            detected_language=func.coalesce(
                                Transcription.detected_language, final.language
                            ),
                            decode_strategy=final.decode_strategy[:32],
                            is_edited=False,
                        )
                    )
            except IntegrityError as exc:
                if _DUPLICATE_SEGMENT in str(exc.orig):
                    return SaveOutcome.ALREADY_SAVED  # whole transaction rolled back
                raise
            return SaveOutcome.SAVED

        return await self._with_retries(operation, what=f"save segment {final.seq}")

    async def saved_seqs(self, transcription_id: uuid.UUID) -> list[int]:
        async with self._sessions() as session:
            result = await session.execute(
                select(TranscriptionSegment.seq)
                .where(TranscriptionSegment.transcription_id == transcription_id)
                .order_by(TranscriptionSegment.seq)
            )
            return list(result.scalars())

    # ---------------------------------------------------------------- retries

    async def _with_retries[T](self, operation: Callable[[], Awaitable[T]], *, what: str) -> T:
        attempts = len(self._retry_delays) + 1
        for attempt in range(attempts):
            try:
                return await operation()
            except Exception as exc:
                if attempt == attempts - 1:
                    logger.error(
                        "Could not %s after %d attempts: %s", what, attempts, type(exc).__name__
                    )
                    raise
                logger.warning("Retrying %s after %s", what, type(exc).__name__)
                await asyncio.sleep(self._retry_delays[attempt])
        raise AssertionError("unreachable")  # pragma: no cover
