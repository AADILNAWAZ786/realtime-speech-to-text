"""Speech recognition: audio validation, engine interface, faster-whisper backend,
language modes, results and output-script rendering.

Importing this package does not import faster-whisper; the model is loaded lazily.
"""

from app.speech.audio import AudioBuffer, decode_pcm16, load_audio_file, validate_float_audio
from app.speech.config import MixedStrategy, SpeechEngineConfig, SpeechSettings
from app.speech.engine import DecodeOptions, EngineInfo, SpeechEngine
from app.speech.errors import (
    AudioValidationError,
    InferenceError,
    ModelLoadError,
    SpeechError,
)
from app.speech.faster_whisper_engine import FasterWhisperEngine
from app.speech.language import LanguageDetection, decide_session_language, plan_for_mode
from app.speech.results import TranscriptionResult, TranscriptSegment, WordTiming
from app.speech.script import ScriptRendering, render

__all__ = [
    "AudioBuffer",
    "AudioValidationError",
    "DecodeOptions",
    "EngineInfo",
    "FasterWhisperEngine",
    "InferenceError",
    "LanguageDetection",
    "MixedStrategy",
    "ModelLoadError",
    "ScriptRendering",
    "SpeechEngine",
    "SpeechEngineConfig",
    "SpeechError",
    "SpeechSettings",
    "TranscriptSegment",
    "TranscriptionResult",
    "WordTiming",
    "decide_session_language",
    "decode_pcm16",
    "load_audio_file",
    "plan_for_mode",
    "render",
    "validate_float_audio",
]
