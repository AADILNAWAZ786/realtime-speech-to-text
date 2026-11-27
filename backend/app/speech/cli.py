"""Manual evaluation CLI: transcribe one audio file and print the result.

Entry point: backend/scripts/transcribe_file.py (see docs/speech-evaluation.md).

Exit codes: 0 ok · 2 bad arguments/config · 3 audio error · 4 model load error ·
5 inference error.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import TextIO

from pydantic import ValidationError

from app.models.enums import ScriptPreference, SpeechMode
from app.speech.audio import load_audio_file
from app.speech.config import MULTILINGUAL_MODEL_SIZES, MixedStrategy, SpeechSettings
from app.speech.engine import DecodeOptions, SpeechEngine
from app.speech.errors import AudioValidationError, InferenceError, ModelLoadError
from app.speech.faster_whisper_engine import FasterWhisperEngine
from app.speech.results import TranscriptionResult
from app.speech.script import render, script_profile

EXIT_OK, EXIT_USAGE, EXIT_AUDIO, EXIT_MODEL, EXIT_INFERENCE = 0, 2, 3, 4, 5

EngineFactory = Callable[..., SpeechEngine]
PRESETS: dict[str, Callable[[], DecodeOptions]] = {
    "final": DecodeOptions.final,
    "partial": DecodeOptions.partial,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="transcribe_file.py",
        description="Transcribe an audio file with faster-whisper (manual evaluation tool).",
    )
    parser.add_argument("audio", type=Path, help="Audio file (WAV/MP3/M4A/...; resampled)")
    parser.add_argument(
        "--model",
        help=f"Model size, repo id or local path (default: WHISPER_MODEL or 'small'). "
        f"Multilingual sizes: {', '.join(MULTILINGUAL_MODEL_SIZES)}",
    )
    parser.add_argument(
        "--mode",
        choices=[m.value for m in SpeechMode],
        default=SpeechMode.AUTO.value,
        help="auto | en | hi | mixed (Hindi+English)",
    )
    parser.add_argument("--device", choices=["auto", "cpu", "cuda"], help="default: cpu")
    parser.add_argument("--compute-type", help="default: int8 on CPU, float16 on CUDA")
    parser.add_argument("--threads", type=int, help="CPU threads (0 = CTranslate2 default)")
    parser.add_argument(
        "--mixed-strategy",
        choices=[s.value for s in MixedStrategy],
        help="Decoding strategy for --mode mixed (default: hi_prompted)",
    )
    parser.add_argument(
        "--script",
        choices=[s.value for s in ScriptPreference],
        default=ScriptPreference.AS_RECOGNISED.value,
        help="Also show the text in this output script (recognised text is always shown)",
    )
    parser.add_argument("--format", choices=["text", "json"], default="text", dest="fmt")
    parser.add_argument(
        "--preset",
        choices=sorted(PRESETS),
        default="final",
        help="final = beam 5 + VAD + word timings; partial = fast greedy preview settings",
    )
    parser.add_argument("--no-vad", action="store_true", help="Disable faster-whisper's VAD")
    parser.add_argument("--download-root", type=Path, help="Model cache directory")
    parser.add_argument(
        "--local-files-only", action="store_true", help="Never download model weights"
    )
    return parser


def _format_ms(ms: int) -> str:
    minutes, rest = divmod(ms, 60_000)
    return f"{minutes:02d}:{rest / 1000:06.3f}"


def _print_text_report(
    out: TextIO,
    *,
    audio_path: Path,
    result: TranscriptionResult,
    engine: SpeechEngine,
    script: ScriptPreference,
) -> None:
    info = engine.info
    rtf = result.real_time_factor
    if result.language_forced:
        language = f"{result.language} (forced by mode)"
    else:
        language = f"{result.language} (detected, p={result.language_probability or 0:.2f})"
    profile = script_profile(result.text)
    lines: list[str | None] = [
        f"File:          {audio_path} ({result.audio_duration_ms / 1000:.2f} s)",
        f"Model:         {info.model_name} [{info.device}, {info.compute_type}, "
        f"threads={info.cpu_threads or 'default'}]",
        f"Model load:    {info.load_ms / 1000:.2f} s" if info.load_ms is not None else None,
        f"Mode:          {result.speech_mode.value} (strategy: {result.decode_strategy})",
        f"Language:      {language}",
        f"Processing:    {result.processing_ms / 1000:.2f} s"
        + (f" (real-time factor {rtf:.2f})" if rtf is not None else ""),
        f"Script mix:    devanagari={profile.devanagari_chars} latin={profile.latin_chars}",
        "",
        "Text (as recognised):",
        f"  {result.text or '(no speech recognised)'}",
    ]
    if script is not ScriptPreference.AS_RECOGNISED:
        rendering = render(result.text, script)
        lines += [
            "",
            f"Text ({script.value}, method={rendering.method.value}):",
            f"  {rendering.text or '(empty)'}",
        ]
        lines += [f"  note: {note}" for note in rendering.notes]
    lines += ["", f"Segments ({len(result.segments)}):"]
    lines += [
        f"  [{_format_ms(seg.start_ms)} -> {_format_ms(seg.end_ms)}] "
        f"(logprob {seg.avg_logprob:.2f}) {seg.text}"
        for seg in result.segments
    ]
    out.write("\n".join(line for line in lines if line is not None) + "\n")


def main(
    argv: Sequence[str] | None = None,
    *,
    engine_factory: EngineFactory = FasterWhisperEngine,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    out, err = stdout or sys.stdout, stderr or sys.stderr
    args = build_parser().parse_args(argv)

    try:
        config = SpeechSettings().to_engine_config(
            model=args.model,
            device=args.device,
            compute_type=args.compute_type,
            cpu_threads=args.threads,
            mixed_strategy=args.mixed_strategy,
            download_root=args.download_root,
            local_files_only=True if args.local_files_only else None,
        )
    except ValidationError as exc:
        first = exc.errors()[0]
        err.write(f"error: invalid configuration: {first['msg']}\n")
        return EXIT_USAGE

    try:
        audio = load_audio_file(args.audio, max_duration_seconds=config.max_audio_seconds)
    except AudioValidationError as exc:
        err.write(f"error: {exc} [{exc.code}]\n")
        return EXIT_AUDIO

    engine = engine_factory(config)
    options = PRESETS[args.preset]()
    if args.no_vad:
        options = replace(options, vad_filter=False)

    try:
        engine.load()
        result = engine.transcribe(audio, mode=SpeechMode(args.mode), options=options)
    except ModelLoadError as exc:
        err.write(f"error: {exc} [{exc.code}]\n")
        return EXIT_MODEL
    except InferenceError as exc:
        err.write(f"error: {exc} [{exc.code}]\n")
        return EXIT_INFERENCE

    script = ScriptPreference(args.script)
    if args.fmt == "json":
        payload = {
            "file": str(args.audio),
            "model_load_ms": engine.info.load_ms,
            **result.to_dict(),
            "script": _rendering_dict(result.text, script),
        }
        out.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    else:
        _print_text_report(out, audio_path=args.audio, result=result, engine=engine, script=script)
    return EXIT_OK


def _rendering_dict(text: str, script: ScriptPreference) -> dict[str, object]:
    rendering = render(text, script)
    return {
        "preference": rendering.preference.value,
        "method": rendering.method.value,
        "text": rendering.text,
        "changed": rendering.changed,
        "notes": list(rendering.notes),
    }


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
