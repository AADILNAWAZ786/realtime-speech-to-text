#!/usr/bin/env python3
"""Transcribe an audio file with faster-whisper - manual evaluation tool.

Usage (from the backend/ directory):
    python scripts/transcribe_file.py recording.wav --model small --mode mixed
    python scripts/transcribe_file.py clip.m4a --mode hi --script roman --format json

See docs/speech-evaluation.md for the full evaluation procedure.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Allow running as a plain script: make the backend package importable.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.speech.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
