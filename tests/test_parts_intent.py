from __future__ import annotations

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
