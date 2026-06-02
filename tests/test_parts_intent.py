from __future__ import annotations

from autostop_manager.parts_intent import normalize_part_intent


def test_normalize_part_intent_recognizes_front_brake_pads():
    result = normalize_part_intent("передние колодки")

    assert result["recognized"] is True
    assert result["intent_id"] == "front_brake_pads"
    assert "brake_system" in result["critical_vehicle_fields"]
    assert "передние тормозные колодки" in result["catalog_search_terms"]
    assert result["quantity_basis"] == "axle_set"


def test_normalize_part_intent_unknown_keeps_search_text():
    result = normalize_part_intent("редкая штука", axle="front")

    assert result["recognized"] is False
    assert result["catalog_search_terms"] == ["редкая штука"]
    assert result["positions"] == ["front"]


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


def test_injector_washer_is_not_misclassified_as_full_fuel_injector():
    result = normalize_part_intent("шайба форсунки")

    assert result["recognized"] is True
    assert result["intent_id"] == "injector_seal_washer"
