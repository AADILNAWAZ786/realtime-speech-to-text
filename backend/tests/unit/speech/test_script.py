from __future__ import annotations

import pytest

from app.models.enums import ScriptPreference
from app.speech.script import (
    ScriptMethod,
    devanagari_to_roman,
    render,
    script_profile,
)

HINGLISH = "आज की meeting cancel हो गई।"


@pytest.mark.parametrize(
    ("devanagari", "roman"),
    [
        ("आज", "aaj"),
        ("कल", "kal"),  # final schwa deleted
        ("बात", "baat"),
        ("नहीं", "nahin"),
        ("हाँ", "haan"),  # chandrabindu
        ("ठीक है", "thik hai"),
        ("चलो", "chalo"),  # IAST c -> ch
        ("छोटा", "chhotaa"),  # IAST ch -> chh
        ("शाम", "shaam"),
        ("ज़रूर", "zarur"),  # nukta
        ("ख़बर", "khabar"),
        ("पढ़ाई", "parhaai"),  # retroflex flap without diacritics
        ("ऑफ़िस", "ofis"),  # ऑ is not covered by IAST
        ("न", "na"),  # single letter keeps its vowel
        ("क्या", "kyaa"),
    ],
)
def test_devanagari_to_roman_words(devanagari: str, roman: str) -> None:
    assert devanagari_to_roman(devanagari) == roman


def test_roman_output_is_plain_ascii_for_common_words() -> None:
    text = "हाँ, ठीक है। कल ख़बर पढ़ाई शाम ऑफ़िस ज़रूर"
    assert devanagari_to_roman(text).isascii()


def test_mixed_text_keeps_latin_words_digits_and_punctuation() -> None:
    assert devanagari_to_roman(HINGLISH) == "aaj ki meeting cancel ho gai."
    assert devanagari_to_roman("२०२६ में call, OK?") == "2026 men call, OK?"
    assert devanagari_to_roman("ठीक है॥") == "thik hai."


def test_known_limitations_are_deterministic_not_magic() -> None:
    # English loanwords written in Devanagari keep their Hindi spelling...
    assert devanagari_to_roman("मीटिंग") == "miting"
    # ...and medial schwa deletion is not implemented ("karte" colloquially).
    assert devanagari_to_roman("करते") == "karate"


def test_render_as_recognised_is_identity() -> None:
    rendering = render(HINGLISH, ScriptPreference.AS_RECOGNISED)
    assert rendering.text == rendering.source_text == HINGLISH
    assert rendering.method is ScriptMethod.IDENTITY
    assert not rendering.changed


def test_render_roman_is_labelled_as_transliteration_and_keeps_source() -> None:
    rendering = render(HINGLISH, "roman")
    assert rendering.text == "aaj ki meeting cancel ho gai."
    assert rendering.source_text == HINGLISH  # raw recognised text preserved
    assert rendering.method is ScriptMethod.DETERMINISTIC_TRANSLITERATION
    assert rendering.changed
    assert any("not re-recognition" in note for note in rendering.notes)


def test_render_roman_on_latin_text_is_identity() -> None:
    rendering = render("kal meeting hai", ScriptPreference.ROMAN)
    assert rendering.text == "kal meeting hai"
    assert rendering.method is ScriptMethod.IDENTITY


def test_render_devanagari_never_converts_latin() -> None:
    rendering = render(HINGLISH, ScriptPreference.DEVANAGARI)
    assert rendering.text == HINGLISH
    assert rendering.method is ScriptMethod.IDENTITY
    assert any("Latin -> Devanagari is not attempted" in note for note in rendering.notes)
    assert render("आज मौसम अच्छा है", ScriptPreference.DEVANAGARI).notes == ()


def test_render_does_not_mutate_input_and_result_is_frozen() -> None:
    source = HINGLISH
    rendering = render(source, ScriptPreference.ROMAN)
    assert source == "आज की meeting cancel हो गई।"
    with pytest.raises(AttributeError):
        rendering.text = "x"  # type: ignore[misc]


def test_render_rejects_unknown_preference() -> None:
    with pytest.raises(ValueError):
        render("text", "klingon")


def test_script_profile() -> None:
    profile = script_profile(HINGLISH)
    assert profile.devanagari_chars > 0 and profile.latin_chars == len("meetingcancel")
    assert profile.is_mixed
    assert 0 < profile.devanagari_ratio < 1
    assert script_profile("hello").devanagari_ratio == 0.0
    assert script_profile("123 ...").total == 0
