"""Speech modes -> Whisper decoding plans, and the auto-detection decision.

What Whisper does and does not do (see docs/speech-evaluation.md):
- It decodes each window in ONE language. Mid-sentence code-switching (Hinglish) is not
  modelled explicitly; results must be measured, not assumed.
- With language="hi" the output is mostly Devanagari; English words may appear in Latin
  script or transliterated into Devanagari. It does not reliably produce romanised Hinglish.
- Hindi speech is sometimes detected as Urdu ("ur"), which is written in Perso-Arabic script.

This module only decides *how to ask* the model. Output script conversion is a separate,
deterministic step (app.speech.script) and never alters the recognised text.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.models.enums import SpeechMode
from app.speech.config import MixedStrategy

# A short mixed-script example nudging Whisper towards keeping English words in Latin
# script within Hindi (Devanagari) text. A hypothesis to evaluate, not a guarantee.
DEFAULT_MIXED_PROMPT = "हाँ, आज की meeting cancel हो गई। कल फिर call करते हैं, okay?"

HINDI_FAMILY = ("hi", "ur")  # Whisper often confuses Hindi and Urdu speech


@dataclass(frozen=True, slots=True)
class DecodingPlan:
    """How to call the model for a speech mode."""

    mode: SpeechMode
    language: str | None  # None = let Whisper detect
    multilingual: bool  # per-segment language detection
    style_prompt: str | None
    strategy: str  # stored in transcriptions.decode_strategy (<= 32 chars)

    def build_prompt(self, context: str | None = None) -> str | None:
        """Style prompt followed by previous transcript context (if any)."""
        parts = [part.strip() for part in (self.style_prompt, context) if part and part.strip()]
        return " ".join(parts) or None


def plan_for_mode(
    mode: SpeechMode,
    *,
    mixed_strategy: MixedStrategy = MixedStrategy.HINDI_PROMPTED,
    mixed_prompt: str | None = None,
) -> DecodingPlan:
    if mode is SpeechMode.ENGLISH:
        return DecodingPlan(mode, "en", False, None, "en")
    if mode is SpeechMode.HINDI:
        return DecodingPlan(mode, "hi", False, None, "hi")
    if mode is SpeechMode.AUTO:
        return DecodingPlan(mode, None, False, None, "auto")

    # Mixed Hindi + English
    strategy = f"mixed:{mixed_strategy.value}"
    if mixed_strategy is MixedStrategy.HINDI_PROMPTED:
        return DecodingPlan(mode, "hi", False, mixed_prompt or DEFAULT_MIXED_PROMPT, strategy)
    if mixed_strategy is MixedStrategy.HINDI:
        return DecodingPlan(mode, "hi", False, None, strategy)
    if mixed_strategy is MixedStrategy.MULTILINGUAL:
        return DecodingPlan(mode, None, True, mixed_prompt, strategy)
    return DecodingPlan(mode, None, False, mixed_prompt, strategy)


@dataclass(frozen=True, slots=True)
class LanguageDetection:
    language: str
    probability: float
    top: tuple[tuple[str, float], ...]  # most likely first

    def probability_of(self, *codes: str) -> float:
        wanted = set(codes)
        return sum(prob for code, prob in self.top if code in wanted)


@dataclass(frozen=True, slots=True)
class SessionLanguageDecision:
    """Which mode a live session should lock to after detecting on its first speech."""

    mode: SpeechMode
    reason: str


def decide_session_language(
    detection: LanguageDetection,
    *,
    confidence_threshold: float = 0.7,
    mixed_min_probability: float = 0.15,
    mixed_max_gap: float = 0.35,
) -> SessionLanguageDecision:
    """Turn one language detection into a session-wide mode (used by 'auto' sessions).

    Heuristic, to be tuned in the evaluation:
    - Hindi(+Urdu) and English both plausible and close -> MIXED
    - one of them clearly dominant -> HINDI or ENGLISH
    - anything else (another language, or low confidence) -> stay AUTO (detect per call)
    """
    p_hindi = detection.probability_of(*HINDI_FAMILY)
    p_english = detection.probability_of("en")

    if (
        p_hindi >= mixed_min_probability
        and p_english >= mixed_min_probability
        and abs(p_hindi - p_english) <= mixed_max_gap
    ):
        return SessionLanguageDecision(
            SpeechMode.MIXED, f"hindi={p_hindi:.2f} and english={p_english:.2f} are close"
        )
    if p_hindi >= confidence_threshold:
        return SessionLanguageDecision(SpeechMode.HINDI, f"hindi={p_hindi:.2f}")
    if p_english >= confidence_threshold:
        return SessionLanguageDecision(SpeechMode.ENGLISH, f"english={p_english:.2f}")
    return SessionLanguageDecision(
        SpeechMode.AUTO,
        f"no confident hi/en decision (top={detection.language}:{detection.probability:.2f})",
    )
