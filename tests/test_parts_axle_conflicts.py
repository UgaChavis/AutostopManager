"""Conflicting axle hints must not produce an unambiguous catalog request."""

import socket

import pytest

from autostop_manager.automotive_parts import normalize_parts_request
from autostop_manager.parts_intent import normalize_part_intent


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*_args, **_kwargs):
        raise AssertionError("no network expected")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)


@pytest.mark.parametrize(
    "raw, hints, missing",
    [
        ("передние тормозные колодки", {"axle": "rear"}, "axle"),
        ("передние колодки", {"axle": "unknown"}, "axle"),
        ("колодки спереди", {"axle": "rear"}, "axle"),
        ("колодки сзади", {"axle": "front"}, "axle"),
        ("амортизатор спереди", {"axle": "rear"}, "axle"),
        ("амортизатор сзади", {"axle": "front"}, "axle"),
        ("rear brake pads", {"axle": "front"}, "axle"),
        ("передние колодки", {"position": "rear"}, "axle"),
        ("тормозные колодки", {"axle": "unknown"}, "axle"),
        ("тормозные колодки", {"axle": "front/rear"}, "split_by_axle"),
        ("тормозные колодки", {"position": "front rear"}, "split_by_axle"),
        ("колодки", {"axle": "обе оси"}, "split_by_axle"),
        ("колодки", {"axle": "both axles"}, "split_by_axle"),
        ("передние и задние колодки", {"axle": "front"}, "split_by_axle"),
        ("передние и задние амортизаторы", {"axle": "front"}, "split_by_axle"),
        ("амортизаторы перед и зад", {"axle": "front"}, "split_by_axle"),
        ("амортизаторы спереди и сзади", {"axle": "front"}, "split_by_axle"),
        ("front and rear shock absorbers", {"axle": "rear"}, "split_by_axle"),
    ],
)
def test_conflicting_axle_stays_visible_and_blocks_selection(raw, hints, missing):
    intent = normalize_part_intent(raw, **hints)
    assert intent["raw"] == raw
    assert intent["clarification_required"] is True
    assert missing in intent["clarification_fields"]
    assert missing in intent["required_position_fields"]
    assert intent["partsapi_category_candidates"] == []
    assert intent["catalog_search_terms"] == [raw]
    for field, value in hints.items():
        assert intent["explicit_position_context"][field] == value

    request = normalize_parts_request(items=[{"raw": raw, "quantity": 1, "unit": "piece", **hints}])
    item = request["data"]["items"][0]
    assert request["outcome"] == "partial"
    assert item["raw"] == raw
    assert missing in item["missing_fields"]
    assert f"{item['item_id']}.{missing}" in request["missing_fields"]
    for field, value in hints.items():
        assert item[field] == value


@pytest.mark.parametrize(
    "raw, hints",
    [
        ("передние колодки", {"axle": "front"}),
        ("передние колодки", {"axle": "front_axle"}),
        ("rear brake pads", {"axle": "rear"}),
        ("тормозные колодки", {"axle": "передняя"}),
        ("тормозные колодки", {"axle": "передней оси"}),
        ("тормозные колодки", {"axle": "спереди"}),
        ("колодки спереди", {"axle": "передней оси"}),
        ("колодки сзади", {"axle": "задней оси"}),
        ("колодки спереди", {}),
        ("тормозные колодки", {"position": "спереди"}),
        ("тормозные колодки", {"axle": "перед"}),
        ("brake pads", {"axle": "REAR"}),
        ("rear brake pads for both sides", {"axle": "rear"}),
        ("front shock absorbers both sides", {"axle": "front", "side": "left"}),
        ("передняя правая амортизационная стойка", {}),
    ],
)
def test_agreeing_or_inferred_axle_keeps_existing_success(raw, hints):
    request = normalize_parts_request(items=[{"raw": raw, "quantity": 1, "unit": "piece", **hints}])
    assert request["outcome"] == "success"
    assert request["data"]["items"][0]["intent"]["clarification_required"] is False


def test_existing_unspecified_and_two_axles_contract_stays_unchanged():
    for raw in ("тормозные колодки", "передние и задние колодки"):
        request = normalize_parts_request(items=[{"raw": raw, "quantity": 1, "unit": "set"}])
        assert request["outcome"] == "success"
        assert request["data"]["items"][0]["intent"]["clarification_required"] is True
