"""Speech engine configuration.

`SpeechEngineConfig` is the validated, immutable configuration the engine is built from.
`SpeechSettings` loads the same values from `WHISPER_*` environment variables (or `.env`),
independently of the web app's Settings, so the CLI works without a database or JWT secret.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Official multilingual checkpoints (CTranslate2 conversions published for faster-whisper).
MULTILINGUAL_MODEL_SIZES = (
    "tiny",
    "base",
    "small",
    "medium",
    "large-v1",
    "large-v2",
    "large-v3",
    "large-v3-turbo",
    "turbo",
)
# English-only checkpoints: they cannot transcribe Hindi, so they are rejected outright.
ENGLISH_ONLY_MODELS = frozenset(
    {
        "tiny.en",
        "base.en",
        "small.en",
        "medium.en",
        "distil-small.en",
        "distil-medium.en",
        "distil-large-v2",
        "distil-large-v3",
        "distil-large-v3.5",
    }
)

Device = Literal["auto", "cpu", "cuda"]
ComputeType = Literal[
    "auto",
    "int8",
    "int8_float32",
    "int16",
    "float32",
    "float16",
    "int8_float16",
    "bfloat16",
    "int8_bfloat16",
]


class MixedStrategy(StrEnum):
    """How 'mixed' (Hindi + English) speech is decoded. These are hypotheses to be
    compared in the evaluation (docs/speech-evaluation.md), not proven choices."""

    HINDI_PROMPTED = "hi_prompted"  # language=hi + a mixed-script style prompt (default)
    HINDI = "hi"  # language=hi, no prompt
    AUTO_DETECT = "auto"  # language=None: Whisper detects per call
    MULTILINGUAL = "multilingual"  # language=None + per-segment detection


def is_english_only_model(name: str) -> bool:
    base = name.strip().rstrip("/").split("/")[-1].lower()
    for prefix in ("faster-whisper-", "whisper-"):
        base = base.removeprefix(prefix)
    return base in ENGLISH_ONLY_MODELS or base.endswith(".en")


class SpeechEngineConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    model: str = Field(
        default="small",
        min_length=1,
        description="Model size (e.g. small), a Hugging Face repo id, or a local CTranslate2 "
        "model directory. Must be multilingual.",
    )
    device: Device = "cpu"
    device_index: int = Field(default=0, ge=0)
    compute_type: ComputeType = Field(
        default="auto", description="auto = int8 on CPU, float16 on CUDA"
    )
    cpu_threads: int = Field(default=0, ge=0, le=256, description="0 = CTranslate2 default")
    num_workers: int = Field(
        default=1, ge=1, le=16, description="Concurrent transcriptions the model can run"
    )
    download_root: Path | None = Field(
        default=None, description="Model cache directory (default: Hugging Face cache)"
    )
    local_files_only: bool = Field(
        default=False, description="Never download; fail if the model is not cached"
    )
    max_audio_seconds: float = Field(
        default=600.0, gt=0, description="Reject longer inputs before inference"
    )
    mixed_strategy: MixedStrategy = MixedStrategy.HINDI_PROMPTED
    mixed_prompt: str | None = Field(
        default=None,
        max_length=500,
        description="Overrides the built-in style prompt for mixed mode",
    )

    @field_validator("model")
    @classmethod
    def _reject_english_only(cls, value: str) -> str:
        name = value.strip()
        if is_english_only_model(name):
            raise ValueError(
                f"{name!r} is an English-only Whisper model and cannot transcribe Hindi; "
                f"use a multilingual model such as: {', '.join(MULTILINGUAL_MODEL_SIZES)}"
            )
        return name


class SpeechSettings(BaseSettings):
    """`WHISPER_*` environment variables, e.g. WHISPER_MODEL=small, WHISPER_DEVICE=cpu."""

    model_config = SettingsConfigDict(
        env_prefix="WHISPER_", env_file=".env", extra="ignore", hide_input_in_errors=True
    )

    model: str = "small"
    device: Device = "cpu"
    device_index: int = 0
    compute_type: ComputeType = "auto"
    cpu_threads: int = 0
    num_workers: int = 1
    download_root: Path | None = None
    local_files_only: bool = False
    max_audio_seconds: float = 600.0
    mixed_strategy: MixedStrategy = MixedStrategy.HINDI_PROMPTED
    mixed_prompt: str | None = None

    def to_engine_config(self, **overrides: object) -> SpeechEngineConfig:
        values = self.model_dump()
        values.update({key: value for key, value in overrides.items() if value is not None})
        return SpeechEngineConfig.model_validate(values)
