from __future__ import annotations

import math
import uuid
from datetime import datetime
from typing import Annotated, Self

from pydantic import BaseModel, Field, computed_field, model_validator

from app.models.enums import (
    TEXT_MAX_LENGTH,
    TITLE_MAX_LENGTH,
    ScriptPreference,
    SpeechMode,
    TranscriptionSource,
    TranscriptionStatus,
)
from app.schemas.common import NonBlankStr, RequestModel, ResponseModel

PREVIEW_LENGTH = 200
PAGE_SIZE_DEFAULT = 20
PAGE_SIZE_MAX = 100
SEARCH_MAX_LENGTH = 200

TitleStr = Annotated[NonBlankStr, Field(max_length=TITLE_MAX_LENGTH)]
TextStr = Annotated[NonBlankStr, Field(max_length=TEXT_MAX_LENGTH)]


# ---------- Requests ----------


class TranscriptionCreate(RequestModel):
    """Manual creation. (Live recordings are created by the WebSocket pipeline later.)"""

    title: TitleStr | None = Field(
        default=None, description="Defaults to a timestamped title when omitted"
    )
    text: TextStr
    speech_mode: SpeechMode = SpeechMode.AUTO
    script_preference: ScriptPreference = ScriptPreference.AS_RECOGNISED


class TranscriptionUpdate(RequestModel):
    """Partial update. Only `title` and `text` are editable; at least one is required."""

    title: TitleStr | None = None
    text: TextStr | None = None

    @model_validator(mode="after")
    def _at_least_one_non_null_field(self) -> Self:
        if not self.model_fields_set:
            raise ValueError("Provide at least one of: title, text")
        for name in self.model_fields_set:
            if getattr(self, name) is None:
                raise ValueError(f"{name} cannot be null")
        return self


class TranscriptionListParams(BaseModel):
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=PAGE_SIZE_DEFAULT, ge=1, le=PAGE_SIZE_MAX)
    q: str | None = Field(default=None, max_length=SEARCH_MAX_LENGTH)
    speech_mode: SpeechMode | None = None
    status: TranscriptionStatus | None = None
    source: TranscriptionSource | None = None

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size

    @property
    def search(self) -> str | None:
        if self.q is None:
            return None
        return self.q.strip() or None


# ---------- Responses ----------


class SegmentRead(ResponseModel):
    seq: int
    start_ms: int
    end_ms: int
    raw_text: str
    language: str | None
    avg_logprob: float | None


class _TranscriptionBase(ResponseModel):
    id: uuid.UUID
    title: str
    speech_mode: SpeechMode
    script_preference: ScriptPreference
    source: TranscriptionSource
    status: TranscriptionStatus
    detected_language: str | None
    duration_ms: int | None
    is_edited: bool
    created_at: datetime
    updated_at: datetime


class TranscriptionSummary(_TranscriptionBase):
    """Dashboard list item: a short preview instead of the full text."""

    text: str = Field(exclude=True)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def preview(self) -> str:
        if len(self.text) <= PREVIEW_LENGTH:
            return self.text
        return self.text[:PREVIEW_LENGTH].rstrip() + "…"


class TranscriptionRead(_TranscriptionBase):
    text: str
    decode_strategy: str | None
    model_name: str | None
    segments: list[SegmentRead] | None = Field(
        default=None, description="Present only when requested with include_segments=true"
    )


class TranscriptionPage(BaseModel):
    items: list[TranscriptionSummary]
    total: int
    page: int
    page_size: int

    @computed_field  # type: ignore[prop-decorator]
    @property
    def pages(self) -> int:
        return math.ceil(self.total / self.page_size) if self.total else 0
