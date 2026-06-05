from __future__ import annotations

from autostop_manager.partsapi_category_index import (
    explain_partsapi_category_for_intent,
    search_partsapi_category_index,
    validate_partsapi_category_index,
)


def test_category_index_maps_front_brake_pads_to_numeric_cat():
    result = explain_partsapi_category_for_intent("front_brake_pads", query="передние колодки")

    assert result["category_unresolved"] is False
    assert result["selected_category"]["cat_id"].isdigit()
    assert result["selected_category"]["matched_by"]


def test_category_index_search_and_validate_are_safe():
    search = search_partsapi_category_index("стойка стабилизатора", limit=3)
    validation = validate_partsapi_category_index()

    assert search["ok"] is True
    assert search["matches"]
    assert validation["ok"] is True
    assert validation["privacy"]["secret_exposed"] is False
