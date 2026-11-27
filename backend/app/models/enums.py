"""Domain enumerations and field limits shared by models, schemas and services.

Stored as VARCHAR + CHECK constraint (not native PostgreSQL ENUMs), which are easier to
evolve in migrations.
"""

from __future__ import annotations

from enum import StrEnum


class SpeechMode(StrEnum):
    AUTO = "auto"
    ENGLISH = "en"
    HINDI = "hi"
    MIXED = "mixed"  # Hindi + English (Hinglish)


class ScriptPreference(StrEnum):
    AS_RECOGNISED = "as_recognised"
    ROMAN = "roman"
    DEVANAGARI = "devanagari"


class TranscriptionSource(StrEnum):
    LIVE = "live"
    MANUAL = "manual"


class TranscriptionStatus(StrEnum):
    RECORDING = "recording"
    COMPLETED = "completed"
    INTERRUPTED = "interrupted"


EMAIL_MAX_LENGTH = 320
DISPLAY_NAME_MAX_LENGTH = 100
TITLE_MAX_LENGTH = 200
TEXT_MAX_LENGTH = 100_000
LANGUAGE_CODE_MAX_LENGTH = 8


def sql_in(column: str, enum: type[StrEnum]) -> str:
    """Build a CHECK expression like: status IN ('a', 'b')."""
    values = ", ".join(f"'{member.value}'" for member in enum)
    return f"{column} IN ({values})"
