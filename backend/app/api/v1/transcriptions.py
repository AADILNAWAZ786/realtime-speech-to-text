from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Query, Response, status

from app.api.deps import CurrentUser, TranscriptionServiceDep
from app.models.transcription import Transcription
from app.schemas.common import ErrorResponse
from app.schemas.transcription import (
    SegmentRead,
    TranscriptionCreate,
    TranscriptionListParams,
    TranscriptionPage,
    TranscriptionRead,
    TranscriptionSummary,
    TranscriptionUpdate,
)

router = APIRouter(
    prefix="/transcriptions",
    tags=["transcriptions"],
    responses={401: {"model": ErrorResponse, "description": "Missing or invalid token"}},
)

_NOT_FOUND: dict[int | str, dict[str, Any]] = {
    404: {"model": ErrorResponse, "description": "Not found (or not yours)"}
}
_READ_FIELDS = tuple(name for name in TranscriptionRead.model_fields if name != "segments")


def _to_read(transcription: Transcription, *, include_segments: bool = False) -> TranscriptionRead:
    """Serialise field by field: `segments` is lazy="raise" and is only read when it was
    explicitly loaded (include_segments=true)."""
    data = {name: getattr(transcription, name) for name in _READ_FIELDS}
    if include_segments:
        data["segments"] = [SegmentRead.model_validate(seg) for seg in transcription.segments]
    return TranscriptionRead.model_validate(data)


@router.post(
    "",
    response_model=TranscriptionRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a transcription manually",
    responses={422: {"model": ErrorResponse}},
)
async def create_transcription(
    data: TranscriptionCreate, user: CurrentUser, service: TranscriptionServiceDep
) -> TranscriptionRead:
    transcription = await service.create_manual(user, data)
    return _to_read(transcription)


@router.get("", response_model=TranscriptionPage, summary="List your transcription history")
async def list_transcriptions(
    params: Annotated[TranscriptionListParams, Query()],
    user: CurrentUser,
    service: TranscriptionServiceDep,
) -> TranscriptionPage:
    items, total = await service.list(user, params)
    return TranscriptionPage(
        items=[TranscriptionSummary.model_validate(item) for item in items],
        total=total,
        page=params.page,
        page_size=params.page_size,
    )


@router.get(
    "/{transcription_id}",
    response_model=TranscriptionRead,
    summary="Get one transcription",
    responses=_NOT_FOUND,
)
async def get_transcription(
    transcription_id: uuid.UUID,
    user: CurrentUser,
    service: TranscriptionServiceDep,
    include_segments: Annotated[bool, Query(description="Include raw segments")] = False,
) -> TranscriptionRead:
    transcription = await service.get(user, transcription_id, include_segments=include_segments)
    return _to_read(transcription, include_segments=include_segments)


@router.patch(
    "/{transcription_id}",
    response_model=TranscriptionRead,
    summary="Edit title and/or text",
    responses={**_NOT_FOUND, 422: {"model": ErrorResponse}},
)
async def update_transcription(
    transcription_id: uuid.UUID,
    data: TranscriptionUpdate,
    user: CurrentUser,
    service: TranscriptionServiceDep,
) -> TranscriptionRead:
    transcription = await service.update(user, transcription_id, data)
    return _to_read(transcription)


@router.delete(
    "/{transcription_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Delete a transcription",
    responses=_NOT_FOUND,
)
async def delete_transcription(
    transcription_id: uuid.UUID, user: CurrentUser, service: TranscriptionServiceDep
) -> Response:
    await service.delete(user, transcription_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
