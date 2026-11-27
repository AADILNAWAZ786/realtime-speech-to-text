"""Safe cut points for long utterances, boundary de-duplication and hallucination filtering.

Forced commits (utterance reached max length) must neither lose nor duplicate words:

1. Prefer a *silence* cut: the longest non-speech run (>= min_boundary_silence_ms) in the
   last boundary_search_ms. No word can straddle a silence, so nothing is split.
2. Otherwise cut at a *word boundary* chosen from the final pass's word timestamps: commit
   words that end before (end - forced_carry_ms) and carry the remaining audio into the
   next utterance. Every audio sample belongs to exactly one utterance, so a word is
   transcribed once.
3. As a safety net, `dedupe_prefix` drops words at the start of the next final that
   repeat the end of the previous one (Whisper can echo its prompt or a cut-off word).
"""

from __future__ import annotations

import unicodedata
from collections.abc import Sequence

from app.realtime.buffer import Frame
from app.speech.results import TranscriptSegment, WordTiming


def find_silence_cut(
    frames: Sequence[Frame], *, search_from_sample: int, min_silence_samples: int
) -> int | None:
    """Midpoint of the longest non-speech run starting at/after `search_from_sample`.

    Ties go to the latest run (longest committed utterance). Returns None if no run is
    long enough.
    """
    best: tuple[int, int] | None = None  # (length, midpoint)
    run_start: int | None = None
    run_end = 0
    for frame in (*frames, None):
        silent = frame is not None and not frame.is_speech
        if silent and frame is not None and frame.start_sample >= search_from_sample:
            if run_start is None:
                run_start = frame.start_sample
            run_end = frame.end_sample
            continue
        if run_start is not None:
            length = run_end - run_start
            if length >= min_silence_samples and (best is None or length >= best[0]):
                best = (length, run_start + length // 2)
            run_start = None
    return best[1] if best else None


def choose_word_cut(words: Sequence[WordTiming], *, keep_until_ms: int) -> tuple[int, int] | None:
    """Pick (number of words to keep, cut time in ms) for a word-boundary forced commit.

    Keeps every word ending at or before `keep_until_ms`; at least one word must be kept
    and at least one carried, otherwise None (commit everything). The cut sits midway in
    the gap between the last kept word and the next one.
    """
    keep = 0
    for index, word in enumerate(words):
        if word.end_ms <= keep_until_ms:
            keep = index + 1
    if keep == 0 or keep >= len(words):
        return None
    last, following = words[keep - 1], words[keep]
    gap_start, gap_end = last.end_ms, max(following.start_ms, last.end_ms)
    return keep, gap_start + (gap_end - gap_start) // 2


def normalise_token(token: str) -> str:
    """Case-fold and strip punctuation (incl. the Devanagari danda) for comparisons."""
    stripped = "".join(
        ch for ch in token if not unicodedata.category(ch).startswith("P") and ch not in "।॥"
    )
    return unicodedata.normalize("NFC", stripped).casefold().strip()


def dedupe_prefix(
    previous_tail: Sequence[str],
    new_words: Sequence[str],
    *,
    min_words: int = 2,
    max_words: int = 8,
) -> int:
    """How many leading `new_words` repeat the end of `previous_tail` (0 = none).

    Requires an overlap of at least `min_words` so a legitimately repeated single word
    ("haan haan") is kept.
    """
    prev = [t for t in (normalise_token(w) for w in previous_tail) if t]
    new = [normalise_token(w) for w in new_words]
    limit = min(max_words, len(prev), len(new))
    for size in range(limit, min_words - 1, -1):
        if [t for t in new[:size]] == prev[-size:]:
            return size
    return 0


def is_probable_hallucination(
    segment: TranscriptSegment,
    *,
    no_speech_prob: float,
    logprob: float,
    compression_ratio: float,
) -> bool:
    """Whisper's own failure signals: likely silence with low confidence, or repetitive
    text (high compression ratio, e.g. a phrase looping)."""
    if segment.no_speech_prob > no_speech_prob and segment.avg_logprob < logprob:
        return True
    return segment.compression_ratio > compression_ratio
