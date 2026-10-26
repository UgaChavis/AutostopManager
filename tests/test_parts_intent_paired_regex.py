from __future__ import annotations

import socket
import sqlite3

import pytest

from autostop_manager.parts_intent import normalize_part_intent


@pytest.fixture(autouse=True)
def no_external_access(monkeypatch):
    def denied(*_args, **_kwargs):
        raise AssertionError("paired-axle grammar test attempted external access")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(sqlite3, "connect", denied)


@pytest.mark.parametrize(
    "text",
    [
        "передние и задние колодки",
        "задние и передние тормозные колодки",
        "колодки передние, а также задние",
        "передние колодки и также задние",
        "задние колодки / передние колодки",
        "передние \t , \tи \tзадние колодки",
        "front and rear brake pads",
        "rear and also front pads",
        "brake pads front, and rear",
        "front pads, and also rear pads",
        "rear pads & front pads",
        "front + rear pads",
    ],
)
def test_paired_axle_join_forms_retain_their_specific_intent(text):
    result = normalize_part_intent(text)

    assert result["intent_id"] == "brake_pads_multiple_axles"
    assert result["clarification_required"] is True
    assert "split_by_axle" in result["clarification_fields"]


@pytest.mark.parametrize(
    "text",
    [
        "front andalso rear pads",
        "front rear brake pads",
        "front and rearward pads",
        "передние колодки, количество 2 комплекта",
        "передние колодки и задние амортизаторы",
        "задние фильтры и передние колодки",
    ],
)
def test_nearby_words_or_other_parts_do_not_become_paired_brake_pads(text):
    assert normalize_part_intent(text)["intent_id"] != "brake_pads_multiple_axles"


def test_whitespace_before_a_conjunction_is_still_accepted():
    result = normalize_part_intent("передние" + " " * 64 + "и задние колодки")

    assert result["intent_id"] == "brake_pads_multiple_axles"


def test_whitespace_without_a_join_keeps_an_unknown_item_unknown():
    result = normalize_part_intent("передние" + " " * 64 + "деталь 1 шт")

    assert result["intent_id"] == "unknown"
