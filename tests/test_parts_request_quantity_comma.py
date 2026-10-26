from __future__ import annotations

import pytest

from autostop_manager.automotive_parts import normalize_parts_request


@pytest.mark.parametrize(
    ("text", "intent", "quantity", "unit"),
    [
        ("масляный фильтр, 1 шт", "oil_filter", 1, "piece"),
        ("масляный фильтр  , \t1 шт", "oil_filter", 1, "piece"),
        ("масляный фильтр, количество 1 шт", "oil_filter", 1, "piece"),
        ("масляный фильтр, 1,5 шт", "oil_filter", 1.5, "piece"),
        ("неизвестная прокладка, 2 шт", "unknown", 2, "piece"),
        ("масло, 1,5 л", "unknown", 1.5, "liter"),
        ("oil filter, 1 pc", "oil_filter", None, None),
        ("oil filter, 1 pc.", "oil_filter", None, None),
    ],
)
def test_quantity_annotation_stays_with_its_one_raw_item(text, intent, quantity, unit):
    response = normalize_parts_request(text=text)

    assert len(response["data"]["items"]) == 1
    item = response["data"]["items"][0]
    assert item["raw"] == text
    assert item["intent"]["intent_id"] == intent
    assert (item["quantity"], item["unit"]) == (quantity, unit)
    assert item["quantity_status"] == ("unknown" if quantity is None else "known")
    assert response["ok"] is True
    assert response["outcome"] == ("partial" if intent == "unknown" or quantity is None else "success")


@pytest.mark.parametrize("separator", [", ", "; ", "\n"])
def test_actual_item_separators_keep_all_positions_and_quantities(separator):
    response = normalize_parts_request(text="масляный фильтр 1 шт" + separator + "салонный фильтр 2 шт")

    items = response["data"]["items"]
    assert len(items) == 2
    assert [item["intent"]["intent_id"] for item in items] == ["oil_filter", "cabin_filter"]
    assert [(item["quantity"], item["unit"]) for item in items] == [(1, "piece"), (2, "piece")]
    assert [item["raw"] for item in items] == ["масляный фильтр 1 шт", "салонный фильтр 2 шт"]
    assert response["ok"] is True and response["outcome"] == "success"


@pytest.mark.parametrize("separator", [";", "\n"])
def test_quantity_annotation_does_not_cross_a_hard_item_separator(separator):
    response = normalize_parts_request(text="масляный фильтр" + separator + "1 шт")

    items = response["data"]["items"]
    assert len(items) == 2
    assert items[0]["raw"] == "масляный фильтр" and items[0]["quantity"] is None
    assert items[1]["raw"] == "1 шт" and items[1]["intent"]["intent_id"] == "unknown"
    assert (items[1]["quantity"], items[1]["unit"]) == (1, "piece")


@pytest.mark.parametrize(
    "text",
    ["масляный фильтр, 2 шт неизвестная прокладка", "масляный фильтр,2 лампы", "масляный фильтр, 2 штуцера"],
)
def test_quantity_prefix_with_a_part_name_is_a_separate_position(text):
    response = normalize_parts_request(text=text)

    items = response["data"]["items"]
    assert len(items) == 2
    assert items[0]["raw"] == "масляный фильтр"
    assert items[0]["quantity"] is None
    assert items[1]["raw"] == text.split(",", 1)[1].strip()
    assert response["outcome"] == "partial"


def test_second_quantity_is_not_silently_added_to_an_already_quantified_item():
    response = normalize_parts_request(text="масляный фильтр 1 шт, 2 шт")

    items = response["data"]["items"]
    assert len(items) == 2
    assert [(item["quantity"], item["unit"]) for item in items] == [(1, "piece"), (2, "piece")]
    assert items[1]["intent"]["intent_id"] == "unknown"
    assert response["outcome"] == "partial"


@pytest.mark.parametrize("quantity", [0, None])
def test_explicit_items_preserve_quantity_marking_and_unknown_oem_kind(quantity):
    raw = "масляный фильтр, 1 шт"
    response = normalize_parts_request(
        items=[
            {
                "item_id": "synthetic-item",
                "raw": raw,
                "quantity": quantity,
                "unit": "piece",
                "brand": "DEMO",
                "part_number": "DEMO-001",
            }
        ]
    )

    assert len(response["data"]["items"]) == 1
    item = response["data"]["items"][0]
    assert item["item_id"] == "synthetic-item" and item["raw"] == raw
    assert item["quantity"] == quantity and item["unit"] == "piece"
    assert item["quantity_status"] == ("unknown" if quantity is None else "known")
    assert item["brand"] == "DEMO"
    assert item["raw_part_number"] == "DEMO-001" and item["normalized_part_number"] == "DEMO001"
    assert item["number_kind"] == "unknown"
    assert response["ok"] is True
    assert response["outcome"] == ("partial" if quantity is None else "success")
