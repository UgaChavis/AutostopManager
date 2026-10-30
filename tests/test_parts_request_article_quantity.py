"""A labelled numeric article before a spaced dash is not a quantity range."""

import subprocess

import pytest

from test_parts_request_negative_quantity import (
    call as call,
    forbid_external_state as forbid_external_state,
    native_tool as native_tool,
)


@pytest.fixture(autouse=True)
def forbid_subprocess(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("parts request normalization must not start a subprocess")

    monkeypatch.setattr(subprocess, "Popen", denied)


@pytest.mark.parametrize("separator", [" - ", " − ", " – ", " — ", "\u00a0—\u202f"])
def test_explicit_numeric_article_keeps_the_following_quantity(call, separator):
    raw = f"масляный фильтр артикул 12345678{separator}2 шт"
    response = call(text=raw)

    assert response["outcome"] == "success"
    item = response["data"]["items"][0]
    assert item["raw"] == raw and item["quantity"] == 2 and item["unit"] == "piece"
    assert item["quantity_status"] == "known" and item["missing_fields"] == []
    assert item["raw_part_number"] is None and item["normalized_part_number"] is None


@pytest.mark.parametrize("part_number", [None, "12345678"])
def test_explicit_item_metadata_is_preserved(call, part_number):
    raw = "масляный фильтр артикул 12345678 — 2 шт"
    supplied = {"raw": raw, "item_id": "synthetic", "brand": "DEMO"}
    if part_number is not None:
        supplied["part_number"] = part_number
    response = call(items=[supplied])

    assert response["outcome"] == "success"
    item = response["data"]["items"][0]
    assert item["quantity"] == 2 and item["quantity_status"] == "known"
    assert item["item_id"] == "synthetic" and item["brand"] == "DEMO"
    assert item["raw_part_number"] == item["normalized_part_number"] == part_number


@pytest.mark.parametrize(
    "description",
    [
        "12345678 — 2 шт",
        "количество 1 — 2 шт",
        "артикул 12345678 — 2–3 шт",
        "артикул 12345678 — 1/2 шт",
        "артикул 12345678 — количество 1 000 шт",
        "артикул 12345678—2 шт",
        "артикул 12345678— 2 шт",
        "артикул 12345678 —2 шт",
        "артикул -12345678 — 2 шт",
        "артикул 123.5 — 2 шт",
        "неартикул 12345678 — 2 шт",
    ],
)
def test_non_article_or_ambiguous_numbers_stay_unknown(call, description):
    response = call(items=[{"raw": "масляный фильтр " + description, "part_number": "12345678"}])

    assert response["outcome"] == "partial"
    item = response["data"]["items"][0]
    assert item["quantity"] is None and item["quantity_status"] == "unknown"
    assert "quantity" in item["missing_fields"]


def test_zero_quantity_and_leading_zero_article_are_not_rewritten(call):
    response = call(items=[{"raw": "масляный фильтр АРТИКУЛ 00001234 — 0 шт", "part_number": "00001234"}])
    item = response["data"]["items"][0]
    assert response["outcome"] == "success" and item["quantity"] == 0
    assert item["raw_part_number"] == item["normalized_part_number"] == "00001234"


def test_explicit_quantity_still_overrides_ambiguous_raw_text(call):
    response = call(items=[{"raw": "масляный фильтр артикул 12345678 — 2–3 шт", "quantity": 4}])
    assert response["outcome"] == "success"
    assert response["data"]["items"][0]["quantity"] == 4
