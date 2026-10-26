from __future__ import annotations

import pytest

from autostop_manager.automotive_parts import normalize_parts_request


@pytest.mark.parametrize("separator", [" и ", " И ", " а также ", " and ", " ALSO ", " & ", " / ", "+"])
def test_supported_conjunctions_keep_every_named_position(separator):
    response = normalize_parts_request(text="масляный фильтр 1 шт" + separator + "салонный фильтр 2 шт")

    items = response["data"]["items"]
    assert len(items) == 2
    assert [item["raw"] for item in items] == ["масляный фильтр 1 шт", "салонный фильтр 2 шт"]
    assert [item["intent"]["intent_id"] for item in items] == ["oil_filter", "cabin_filter"]
    assert [(item["quantity"], item["unit"]) for item in items] == [(1, "piece"), (2, "piece")]
    assert response["ok"] is True and response["outcome"] == "success"


def test_english_native_counterexample_splits_without_new_quantity_conversion():
    response = normalize_parts_request(text="oil filter 1 piece and cabin filter 2 pieces")

    items = response["data"]["items"]
    assert len(items) == 2
    assert [item["raw"] for item in items] == ["oil filter 1 piece", "cabin filter 2 pieces"]
    assert [item["intent"]["intent_id"] for item in items] == ["oil_filter", "cabin_filter"]
    assert all(item["quantity"] is None and item["unit"] is None for item in items)
    assert all(item["quantity_status"] == "unknown" for item in items)
    assert response["ok"] is True and response["outcome"] == "partial"


@pytest.mark.parametrize(
    ("text", "raw_items", "intents"),
    [
        ("injector seal and washer", ["injector seal and washer"], ["injector_seal_washer"]),
        (
            "injector seal and washer and oil filter",
            ["injector seal and washer", "oil filter"],
            ["injector_seal_washer", "oil_filter"],
        ),
        (
            "oil filter and injector seal and washer",
            ["oil filter", "injector seal and washer"],
            ["oil_filter", "injector_seal_washer"],
        ),
        (
            "injector seal and washer, oil filter",
            ["injector seal and washer", "oil filter"],
            ["injector_seal_washer", "oil_filter"],
        ),
        (
            "oil filter, injector seal and washer",
            ["oil filter", "injector seal and washer"],
            ["oil_filter", "injector_seal_washer"],
        ),
        (
            "фара ближнего и дальнего света и масляный фильтр",
            ["фара ближнего и дальнего света", "масляный фильтр"],
            ["headlight", "oil_filter"],
        ),
        (
            "oil filter with housing and seal and air filter",
            ["oil filter with housing and seal", "air filter"],
            ["oil_filter", "air_filter"],
        ),
        (
            "oil filter with housing and seal, передние колодки",
            ["oil filter with housing and seal", "передние колодки"],
            ["oil_filter", "front_brake_pads"],
        ),
    ],
)
def test_known_names_and_unquantified_attachments_stay_with_their_item(text, raw_items, intents):
    response = normalize_parts_request(text=text)

    items = response["data"]["items"]
    assert len(items) == len(raw_items)
    assert [item["raw"] for item in items] == raw_items
    assert [item["intent"]["intent_id"] for item in items] == intents
    assert response["ok"] is True and response["outcome"] == "partial"


@pytest.mark.parametrize(
    ("text", "raw_items", "quantities"),
    [
        ("масляный фильтр и прокладка", ["масляный фильтр", "прокладка"], [None, None]),
        (
            "масляный фильтр 1 шт и 2 шт неизвестная прокладка",
            ["масляный фильтр 1 шт", "2 шт неизвестная прокладка"],
            [1, 2],
        ),
    ],
)
def test_unknown_named_or_quantified_position_is_not_dropped(text, raw_items, quantities):
    response = normalize_parts_request(text=text)

    items = response["data"]["items"]
    assert len(items) == 2
    assert [item["raw"] for item in items] == raw_items
    assert [item["quantity"] for item in items] == quantities
    assert [item["intent"]["intent_id"] for item in items] == ["oil_filter", "unknown"]
    assert response["ok"] is True and response["outcome"] == "partial"


@pytest.mark.parametrize(
    ("text", "raw_items", "intents"),
    [
        (
            "Straße OIL FILTER AND CABIN FILTER",
            ["Straße OIL FILTER", "CABIN FILTER"],
            ["oil_filter", "cabin_filter"],
        ),
        (
            "ﬃ injector seal and washer and oil filter",
            ["ﬃ injector seal and washer", "oil filter"],
            ["injector_seal_washer", "oil_filter"],
        ),
    ],
)
def test_casefold_expansion_does_not_shift_original_raw_boundaries(text, raw_items, intents):
    response = normalize_parts_request(text=text)

    items = response["data"]["items"]
    assert len(items) == 2
    assert [item["raw"] for item in items] == raw_items
    assert [item["intent"]["intent_id"] for item in items] == intents


@pytest.mark.parametrize(
    ("separator", "quantity_text", "quantity"), [(" and ", "1", 1), (" и ", "1", 1), (" and ", "1,5", 1.5)]
)
def test_conjunctions_split_before_comma_quantity_attachment(separator, quantity_text, quantity):
    first = "масляный фильтр, " + quantity_text + " шт"
    second = "салонный фильтр, 2 шт"
    response = normalize_parts_request(text=first + separator + second)

    items = response["data"]["items"]
    assert len(items) == 2
    assert [item["raw"] for item in items] == [first, second]
    assert [item["intent"]["intent_id"] for item in items] == ["oil_filter", "cabin_filter"]
    assert [(item["quantity"], item["unit"]) for item in items] == [(quantity, "piece"), (2, "piece")]
    assert response["ok"] is True and response["outcome"] == "success"


def test_explicit_items_are_not_implicitly_resplit():
    raw = "oil filter and cabin filter"
    response = normalize_parts_request(items=[raw])

    items = response["data"]["items"]
    assert len(items) == 1
    assert items[0]["raw"] == raw and items[0]["intent"]["intent_id"] == "multiple_parts"
    assert response["ok"] is True and response["outcome"] == "partial"
