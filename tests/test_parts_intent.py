from __future__ import annotations

import pytest

from autostop_manager.parts_intent import normalize_part_intent


def test_normalize_part_intent_recognizes_front_brake_pads():
    result = normalize_part_intent("передние колодки")

    assert result["recognized"] is True
    assert result["intent_id"] == "front_brake_pads"
    assert "brake_system" in result["critical_vehicle_fields"]
    assert "передние тормозные колодки" in result["catalog_search_terms"]
    assert result["quantity_basis"] == "axle_set"


def test_normalize_part_intent_recognizes_brake_pad_phrases_with_brake_word():
    front_cases = ["передние тормозные колодки", "колодки тормозные передние"]
    rear_cases = ["задние тормозные колодки", "колодки тормозные задние"]

    for phrase in front_cases:
        result = normalize_part_intent(phrase)

        assert result["recognized"] is True
        assert result["intent_id"] == "front_brake_pads"

    for phrase in rear_cases:
        result = normalize_part_intent(phrase)

        assert result["recognized"] is True
        assert result["intent_id"] == "rear_brake_pads"


def test_normalize_part_intent_recognizes_unspecified_brake_pads_as_clarification():
    result = normalize_part_intent("тормозные колодки")

    assert result["recognized"] is True
    assert result["intent_id"] == "brake_pads_unspecified_axle"
    assert result["clarification_required"] is True
    assert result["clarification_fields"] == ["axle"]
    assert "clarification_prompt" not in result
    assert "price_basis_hint" not in result
    assert "fitment_caveats" not in result


def test_normalize_part_intent_blocks_single_lookup_for_brake_pads_on_both_axles():
    for phrase in [
        "передние и задние колодки",
        "задние / передние колодки",
        "колодки передние и задние",
        "колодки задние и передние",
        "передние колодки и задние",
        "передние колодки и задние колодки",
        "передние, а также задние колодки",
        "передние и также задние колодки",
        "front and rear brake pads",
        "front and also rear brake pads",
        "rear / front pads",
        "brake pads front and rear",
        "front brake pads and rear",
        "front brake pads, rear brake pads",
    ]:
        result = normalize_part_intent(phrase)

        assert result["intent_id"] == "brake_pads_multiple_axles"
        assert result["positions"] == ("front_and_rear_axles",)
        assert result["clarification_required"] is True
        assert result["clarification_fields"] == ["split_by_axle"]
        assert result["partsapi_category_candidates"] == []


@pytest.mark.parametrize(
    ("phrase", "intents"),
    [
        ("передние колодки и задние амортизаторы", {"front_brake_pads", "shock_absorber"}),
        ("задние амортизаторы, передние колодки", {"front_brake_pads", "shock_absorber"}),
        ("front brake pads and rear shock absorbers", {"front_brake_pads", "shock_absorber"}),
        ("колодки и задние амортизаторы", {"brake_pads_unspecified_axle", "shock_absorber"}),
        ("масляный фильтр и салонный фильтр", {"oil_filter", "cabin_filter"}),
        ("свечи накаливания и свечи зажигания", {"glow_plug", "spark_plug"}),
    ],
)
def test_compound_request_preserves_distinct_parts_without_single_category(phrase, intents):
    result = normalize_part_intent(phrase)

    assert result["intent_id"] == "multiple_parts"
    assert result["recognized"] is True
    assert set(result["matched_intents"]) == intents
    assert result["raw"] == phrase
    assert result["catalog_search_terms"] == [phrase]
    assert result["clarification_fields"] == ["split_by_part"]
    assert result["clarification_required"] is True
    assert result["partsapi_category_candidates"] == []


@pytest.mark.parametrize(
    ("phrase", "intent"),
    [
        ("внутренний ШРУС", "inner_cv_joint"),
        ("injector washer", "injector_seal_washer"),
        ("ремень генератора", "belt_tensioner_or_roller"),
    ],
)
def test_specific_part_name_does_not_create_an_extra_part(phrase, intent):
    result = normalize_part_intent(phrase)
    assert result["intent_id"] == intent
    assert result["recognized"] is True


def test_normalize_part_intent_distinguishes_inner_outer_and_unspecified_cv_joints():
    inner = normalize_part_intent("внутренний ШРУС")
    outer = normalize_part_intent("наружный ШРУС")
    unspecified = normalize_part_intent("ШРУС")

    assert inner["intent_id"] == "inner_cv_joint"
    assert outer["intent_id"] == "outer_cv_joint"
    assert unspecified["intent_id"] == "cv_joint_unspecified"
    assert unspecified["clarification_required"] is True
    assert "inner_outer" in unspecified["clarification_fields"]


def test_cv_joint_axle_and_inner_outer_are_independent_coordinates():
    axle_only = normalize_part_intent("ШРУС", side="left", position="front")
    joint_type_only = normalize_part_intent("ШРУС", side="left", position="inner")
    ambiguous_legacy_position = normalize_part_intent("ШРУС", side="left", position="front outer")
    compatible_legacy_pair = normalize_part_intent("ШРУС", axle="front", side="left", position="outer")
    complete = normalize_part_intent("ШРУС", axle="front", side="left", inner_outer="outer")

    assert axle_only["clarification_fields"] == ["inner_outer"]
    assert axle_only["explicit_position_context"] == {"axle": "front", "side": "left", "position": "front"}
    assert joint_type_only["clarification_fields"] == ["axle"]
    assert joint_type_only["explicit_position_context"] == {
        "side": "left",
        "position": "inner",
        "inner_outer": "inner",
    }
    assert ambiguous_legacy_position["clarification_fields"] == ["axle", "inner_outer"]
    assert compatible_legacy_pair["clarification_required"] is False
    assert complete["clarification_required"] is False
    assert complete["explicit_position_context"] == {
        "axle": "front",
        "side": "left",
        "inner_outer": "outer",
    }


def test_normalize_part_intent_does_not_classify_glow_plugs_as_spark_plugs():
    glow = normalize_part_intent("свечи накаливания")
    spark = normalize_part_intent("свечи зажигания")

    assert glow["intent_id"] == "glow_plug"
    assert spark["intent_id"] == "spark_plug"


def test_normalize_part_intent_resolves_structured_clarification_from_context():
    result = normalize_part_intent("тормозные колодки", axle="front")

    assert result["clarification_required"] is False
    assert result["clarification_fields"] == []


def test_normalize_part_intent_recognizes_drive_shaft_with_position_clarification():
    result = normalize_part_intent("приводной вал")

    assert result["recognized"] is True
    assert result["intent_id"] == "drive_shaft"
    assert result["clarification_required"] is True
    assert "side" in result["clarification_fields"]
    assert "axle" in result["clarification_fields"]


def test_normalize_part_intent_extracts_unambiguous_strut_position_from_wording():
    result = normalize_part_intent("передняя правая амортизационная стойка")

    assert result["intent_id"] == "shock_absorber"
    assert result["clarification_required"] is False
    assert result["inferred_position_context"] == {"axle": "front", "side": "right"}


def test_normalize_part_intent_keeps_both_axles_on_clarification_path():
    result = normalize_part_intent("передние и задние амортизаторы")

    assert result["intent_id"] == "shock_absorber"
    assert result["clarification_required"] is True
    assert result["clarification_fields"] == ["axle"]
    assert result["inferred_position_context"] == {}


def test_normalize_part_intent_unknown_keeps_search_text():
    result = normalize_part_intent("редкая штука", axle="front")

    assert result["recognized"] is False
    assert result["catalog_search_terms"] == ["редкая штука"]
    assert result["positions"] == ["front"]
    assert result["clarification_required"] is True
    assert result["clarification_fields"] == ["part_group", "side"]
    assert "clarification_prompt" not in result


def test_normalize_part_intent_ignores_blank_position_context():
    result = normalize_part_intent("редкая штука", axle="  ", side="\t", position="\n")

    assert result["positions"] == []
    assert result["clarification_required"] is True


def test_normalize_part_intent_recognizes_current_crm_part_phrases():
    cases = {
        "свечи зажигания": "spark_plug",
        "компрессор кондиционера": "ac_compressor",
        "топливные форсунки": "fuel_injector",
        "замена ГРМ": "timing_chain_kit",
        "двигатель": "engine_assembly",
        "камера заднего вида": "rear_view_camera",
        "передняя правая ступица": "wheel_hub",
    }

    for phrase, intent_id in cases.items():
        result = normalize_part_intent(phrase)

        assert result["recognized"] is True
        assert result["intent_id"] == intent_id
        assert result["partsapi_cat_candidates"]
        assert result["critical_vehicle_fields"]


def test_headlight_rule_does_not_misclassify_tow_hitch():
    result = normalize_part_intent("фаркоп")

    assert result["recognized"] is False
    assert result["intent_id"] == "unknown"


def test_engine_assembly_rule_does_not_catch_engine_related_service_items():
    for phrase in ["подушка двигателя", "масло двигателя"]:
        result = normalize_part_intent(phrase)

        assert result["recognized"] is False
        assert result["intent_id"] == "unknown"


def test_injector_washer_is_not_misclassified_as_full_fuel_injector():
    result = normalize_part_intent("шайба форсунки")

    assert result["recognized"] is True
    assert result["intent_id"] == "injector_seal_washer"
