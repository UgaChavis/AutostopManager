"""Private synthetic regressions for literal case-independent identifier masking."""

from __future__ import annotations

from copy import deepcopy
import json

import pytest

from autostop_manager import vin_lookup

IDENTIFIER = "AAA12345678901234"
DISPLAY = "AAA***234"
UNRELATED_TEXT = "Проверка: ёж 🚗 / Grüße / Καλημέρα / İıſK"


@pytest.fixture
def forbid_external_inputs(monkeypatch):
    decoder_calls = []

    def forbidden_decoder(*_args, **_kwargs):
        decoder_calls.append(True)
        pytest.fail("the synthetic public plan must not invoke a decoder/provider")

    monkeypatch.setattr(vin_lookup, "load_source_registry", lambda: {"version": 0, "sources": []})
    monkeypatch.setattr(vin_lookup, "sources_for_make", lambda *_args: [])
    monkeypatch.setattr(vin_lookup, "sources_for_inputs", lambda *_args: [])
    monkeypatch.setattr(vin_lookup, "decode_vin_vpic", forbidden_decoder)
    return decoder_calls


@pytest.mark.parametrize(
    "echo", [IDENTIFIER, IDENTIFIER.lower(), "aAa" + IDENTIFIER[3:]], ids=["upper", "lower", "mixed"]
)
def test_public_lookup_plan_masks_case_equivalent_echoes(forbid_external_inputs, echo):
    decode = {
        "ok": True,
        "vehicle": {
            "vin": echo,
            "vehicle_descriptor": echo,
            "make": "SYNTHETIC",
            "model": f"Модель 🚗: {echo}; Grüße",
            "nested": {"items": [f"Эхо — {echo}; ёж", {"memo": f"Copy: {echo}"}]},
            "unrelated": UNRELATED_TEXT,
        },
    }
    before = deepcopy(decode)

    plan = vin_lookup.build_lookup_plan(IDENTIFIER, live_vpic=False, vpic_result=decode)

    assert forbid_external_inputs == []
    assert decode == before
    assert plan["identifier"]["raw_identifier_is_sensitive"] is True
    assert plan["identifier"]["redacted"]["display"] == DISPLAY
    assert plan["decoded_vehicle"]["vin"] == DISPLAY
    assert plan["decoded_vehicle"]["vehicle_descriptor"] == DISPLAY
    assert plan["decoded_vehicle"]["unrelated"] == UNRELATED_TEXT
    assert plan["catalog_routes"] == []
    assert IDENTIFIER.casefold() not in json.dumps(plan, ensure_ascii=False).casefold()
    assert plan["decoded_vehicle"]["model"] == f"Модель 🚗: {DISPLAY}; Grüße"
    assert plan["decoded_vehicle"]["nested"] == {"items": [f"Эхо — {DISPLAY}; ёж", {"memo": f"Copy: {DISPLAY}"}]}


@pytest.mark.parametrize("split_at,case", [(3, "lower"), (6, "mixed")], ids=["split3-lower", "split6-mixed"])
def test_existing_hyphen_variants_are_masked_without_rewriting_other_text(split_at, case):
    hyphenated = IDENTIFIER[:split_at] + "-" + IDENTIFIER[split_at:]
    echo = hyphenated.lower() if case == "lower" else hyphenated[0].lower() + hyphenated[1:]
    text = f"До: {echo}; {UNRELATED_TEXT}"

    result = vin_lookup._redact_identifier_text(text, IDENTIFIER)

    assert result == f"До: {DISPLAY}; {UNRELATED_TEXT}"
    assert text == f"До: {echo}; {UNRELATED_TEXT}"


def test_partial_identifier_star_remains_literal_and_does_not_mask_the_similar_text():
    partial = "AAA123456*8901234"
    without_star = partial.replace("*", "")
    different_character = partial.replace("*", "X")
    text = f"ID={partial}; similar={without_star}; other={different_character}; {UNRELATED_TEXT}"

    result = vin_lookup._redact_identifier_text(text, partial)

    assert result == f"ID={DISPLAY}; similar={without_star}; other={different_character}; {UNRELATED_TEXT}"
    assert text == f"ID={partial}; similar={without_star}; other={different_character}; {UNRELATED_TEXT}"


def test_kelvin_lookalike_is_preserved_while_the_ascii_case_echo_is_masked():
    identifier = "AAK12345678901234"
    unrelated_kelvin = "AAK12345678901234"
    classified = vin_lookup.classify_identifier(unrelated_kelvin, identifier_type="vin")
    assert classified.kind == "unknown"
    assert vin_lookup.normalize_vin(unrelated_kelvin) != identifier
    text = f"ASCII: {identifier.lower()}; Unicode: {unrelated_kelvin}; {UNRELATED_TEXT}"

    result = vin_lookup._redact_identifier_text(text, identifier)

    assert result == f"ASCII: AAK***234; Unicode: {unrelated_kelvin}; {UNRELATED_TEXT}"
    assert text == f"ASCII: {identifier.lower()}; Unicode: {unrelated_kelvin}; {UNRELATED_TEXT}"


def test_long_s_echo_with_the_same_canonical_vin_is_masked():
    identifier = "AAS12345678901234"
    equivalent_echo = "AAſ12345678901234"
    classified = vin_lookup.classify_identifier(equivalent_echo, identifier_type="vin")
    assert classified.kind == "vin"
    assert classified.normalized == identifier
    text = f"ID={equivalent_echo}; {UNRELATED_TEXT}"

    result = vin_lookup._redact_identifier_text(text, identifier)

    assert result == f"ID=AAS***234; {UNRELATED_TEXT}"
    assert text == f"ID={equivalent_echo}; {UNRELATED_TEXT}"


@pytest.mark.parametrize(
    "identifier,equivalent_echo,display",
    [
        ("ASS12345678901234", "Aß12345678901234", "ASS***234"),
        ("AFF12345678901234", "Aﬀ12345678901234", "AFF***234"),
    ],
    ids=["sharp-s-to-SS", "ligature-to-FF"],
)
def test_length_changing_uppercase_echo_with_the_same_canonical_vin_is_masked(identifier, equivalent_echo, display):
    classified = vin_lookup.classify_identifier(equivalent_echo, identifier_type="vin")
    assert len(identifier) == 17
    assert len(equivalent_echo) == 16
    assert classified.kind == "vin"
    assert classified.normalized == identifier
    text = f"ID={equivalent_echo}; {UNRELATED_TEXT}"

    result = vin_lookup._redact_identifier_text(text, identifier)

    assert result == f"ID={display}; {UNRELATED_TEXT}"
    assert text == f"ID={equivalent_echo}; {UNRELATED_TEXT}"


def test_matching_cannot_start_or_end_inside_an_uppercase_expansion():
    identifier = "SAA1234567890123F"
    start_half = "ß" + identifier[1:]
    end_half = identifier[:-1] + "ﬀ"
    different_identifier = "SAB1234567890123F"
    for unrelated in (start_half, end_half):
        classified = vin_lookup.classify_identifier(unrelated, identifier_type="vin")
        assert classified.kind == "unknown"
        assert len(vin_lookup.normalize_vin(unrelated)) == 18
        assert identifier in unrelated.upper()
    text = (
        f"left={start_half}; right={end_half}; different={different_identifier}; "
        f"real={identifier.lower()}; {UNRELATED_TEXT}"
    )

    result = vin_lookup._redact_identifier_text(text, identifier)

    assert result == (
        f"left={start_half}; right={end_half}; different={different_identifier}; real=SAA***23F; {UNRELATED_TEXT}"
    )
    assert text == (
        f"left={start_half}; right={end_half}; different={different_identifier}; "
        f"real={identifier.lower()}; {UNRELATED_TEXT}"
    )
