"""Output script handling, kept strictly separate from speech recognition.

IMPORTANT: this module performs *deterministic transliteration* of text that has already
been recognised. It does not improve, correct or "understand" the transcript:
- Romanising Devanagari maps letters to Latin letters. It cannot recover the original
  English spelling of an English word that the model wrote in Devanagari
  ("मीटिंग" -> "miting", not "meeting").
- Converting Latin text to Devanagari is not offered: a rule cannot tell a romanised
  Hindi word ("kal") from an English one ("call").
- The input text is never modified; a new `ScriptRendering` is returned.

Romanisation pipeline: Hindi pre-processing (final-schwa deletion, ऑ, danda)
-> indic-transliteration (Devanagari -> IAST) -> simplification to plain ASCII for casual
readability ("āja" -> "aaj"). Medial schwa deletion is NOT handled
("समझना" -> "samajhanaa", colloquially "samajhna").
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum

from indic_transliteration import sanscript

from app.models.enums import ScriptPreference

_DEVANAGARI_WORD = re.compile(r"[ऀ-ॣ॰-ॿ꣠-ꣿ]+")
_DEVANAGARI_CHAR = re.compile(r"[ऀ-ॿ꣠-ꣿ]")
_LATIN_LETTER = re.compile(r"[A-Za-zÀ-ɏ]")
_CONSONANTS = re.compile(r"[क-हक़-य़]़?$")  # optional nukta
_VIRAMA = "्"
_DEVANAGARI_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")
_PUNCTUATION = str.maketrans({"।": ".", "॥": "."})  # danda, double danda
# ऑ / ॉ (English "o" as in "office") are not handled by the IAST scheme.
_PRE_REPLACEMENTS = {"ऑ": "ओ", "ॉ": "ो"}

# IAST -> casual ASCII. Order matters (multi-character sequences first).
_SIMPLIFY: tuple[tuple[str, str], ...] = (
    ("k͟h", "kh"),  # ख़
    ("r̤", "r"),  # ड़ / ढ़ (r with diaeresis below)
    ("ch", "\x00"),  # छ (IAST ch) - protect before mapping c -> ch
    ("c", "ch"),  # च (IAST c)
    ("\x00", "chh"),
    ("ā", "aa"),
    ("ī", "i"),
    ("ū", "u"),
    ("ṝ", "ri"),
    ("ṛ", "ri"),
    ("ḷ", "l"),
    ("ś", "sh"),
    ("ṣ", "sh"),
    ("ṃ", "n"),
    ("~", "n"),  # chandrabindu
    ("ṅ", "n"),
    ("ñ", "n"),
    ("ṇ", "n"),
    ("ṭ", "t"),
    ("ḍ", "d"),
    ("ḥ", "h"),
    ("ġ", "gh"),
)


class ScriptMethod(StrEnum):
    IDENTITY = "identity"  # text returned unchanged
    DETERMINISTIC_TRANSLITERATION = "deterministic_transliteration"


@dataclass(frozen=True, slots=True)
class ScriptProfile:
    devanagari_chars: int
    latin_chars: int

    @property
    def total(self) -> int:
        return self.devanagari_chars + self.latin_chars

    @property
    def devanagari_ratio(self) -> float:
        return self.devanagari_chars / self.total if self.total else 0.0

    @property
    def is_mixed(self) -> bool:
        return self.devanagari_chars > 0 and self.latin_chars > 0


@dataclass(frozen=True, slots=True)
class ScriptRendering:
    source_text: str  # the recognised text, untouched
    text: str  # the displayed text
    preference: ScriptPreference
    method: ScriptMethod
    notes: tuple[str, ...] = ()

    @property
    def changed(self) -> bool:
        return self.text != self.source_text


def script_profile(text: str) -> ScriptProfile:
    """Count Devanagari vs Latin letters (used for script-consistency evaluation)."""
    return ScriptProfile(
        devanagari_chars=len(_DEVANAGARI_CHAR.findall(text)),
        latin_chars=len(_LATIN_LETTER.findall(text)),
    )


def _delete_final_schwa(word: str) -> str:
    # Hindi drops the inherent final "a" of multi-letter words ending in a bare consonant
    # (कल = "kal", not "kala"). Adding a virama makes the transliterator do the same.
    if len(word) >= 2 and _CONSONANTS.search(word):
        return word + _VIRAMA
    return word


def _simplify(iast: str) -> str:
    out = iast
    for source, target in _SIMPLIFY:
        out = out.replace(source, target)
    # Strip any remaining combining diacritics (e.g. ż -> z) for plain ASCII output.
    decomposed = unicodedata.normalize("NFD", out)
    return unicodedata.normalize(
        "NFC", "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")
    )


def _romanise_word(match: re.Match[str]) -> str:
    word = match.group(0)
    for source, target in _PRE_REPLACEMENTS.items():
        word = word.replace(source, target)
    iast = sanscript.transliterate(_delete_final_schwa(word), sanscript.DEVANAGARI, sanscript.IAST)
    return _simplify(iast)


def devanagari_to_roman(text: str) -> str:
    """Romanise Devanagari runs; Latin words, digits and punctuation are kept as they are."""
    text = text.translate(_DEVANAGARI_DIGITS).translate(_PUNCTUATION)
    return _DEVANAGARI_WORD.sub(_romanise_word, text)


def render(text: str, preference: ScriptPreference | str) -> ScriptRendering:
    """Render recognised text in the user's preferred output script."""
    pref = ScriptPreference(preference)
    if pref is ScriptPreference.AS_RECOGNISED:
        return ScriptRendering(text, text, pref, ScriptMethod.IDENTITY)

    profile = script_profile(text)
    if pref is ScriptPreference.ROMAN:
        if profile.devanagari_chars == 0:
            return ScriptRendering(
                text, text, pref, ScriptMethod.IDENTITY, ("no Devanagari text to romanise",)
            )
        return ScriptRendering(
            text,
            devanagari_to_roman(text),
            pref,
            ScriptMethod.DETERMINISTIC_TRANSLITERATION,
            (
                "deterministic letter-by-letter transliteration, not re-recognition",
                "English words written in Devanagari keep their Hindi spelling",
            ),
        )

    # DEVANAGARI: keep the recognised text; Latin -> Devanagari is not attempted.
    notes = (
        ("Latin-script words left unchanged (Latin -> Devanagari is not attempted)",)
        if profile.latin_chars
        else ()
    )
    return ScriptRendering(text, text, pref, ScriptMethod.IDENTITY, notes)
