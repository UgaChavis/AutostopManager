"""A shared displacement label cannot corroborate different engine codes."""

from __future__ import annotations

from copy import deepcopy
import socket

import pytest

from autostop_manager import automotive_identity, vehicle_identity
from autostop_manager.automotive_contracts import binding


IDENTIFIER = "WBA" + "0" * 14


@pytest.fixture(autouse=True)
def isolate_sources(monkeypatch):
    def denied(*_args, **_kwargs):
        raise AssertionError("engine comparison must not acquire evidence")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(vehicle_identity, "catalog_provider_status", lambda: {"providers": []})


def compare(route, left, right):
    context = {"make": "BMW", "model": "3 Series", "engine": left}
    provider = {
        "ok": True,
        "error_code": "0",
        "vin": IDENTIFIER,
        "vehicle": {"vin": IDENTIFIER, "make": "BMW", "model": "3 Series", "enginemodel": right},
    }
    before = deepcopy((context, provider))
    if route == "decode":
        result = vehicle_identity.decode_vehicle_identity(
            IDENTIFIER, crm_context=context, vpic_result=provider, live_vpic=False, live_wmi=False
        )
        status = result["field_statuses"]["engine"]["status"]
    else:
        response = automotive_identity.reconcile_vehicle_identity(
            IDENTIFIER,
            [{"input_binding": binding(IDENTIFIER), "vehicle_profile": {"engine": right}}],
            context=context,
            detail=route,
        )
        assert response["execution"] == {"network_calls": 0, "attempts": []}
        result = response["data"]
        status = result["field_statuses"]["engine"]
    assert (context, provider) == before
    return result, status


@pytest.mark.parametrize("route", ["decode", "full", "summary"])
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize(
    "left,right",
    [
        ("2.0TSI EA888", "2.0TSI EA113"),
        ("1.5dCi K9K", "1.5dCi K9L"),
        ("2,0TSI EA888", "2,0TSI EA113"),
        ("2.0TSI", "2.0TSI EA888"),
        ("2.0TSI petrol", "2.0TSI diesel"),
    ],
)
def test_shared_displacement_label_does_not_hide_disagreement(route, reverse, left, right):
    if reverse:
        left, right = right, left
    result, status = compare(route, left, right)
    assert status == "disputed"
    assert "engine" not in result["vehicle_profile"]
    assert any(row["field"] == "engine" for row in result["conflicts"])
    assert result["parts_lookup_readiness"]["ready_for_vehicle_lookup"] is False
    assert result["parts_lookup_readiness"]["ready_for_crm_writeback"] is False


@pytest.mark.parametrize("route", ["decode", "full", "summary"])
@pytest.mark.parametrize(
    "left,right",
    [
        ("M274.920", "M274920"),
        ("R06A", "R06A 658cc gasoline"),
        ("K9K", "K9K 1.5 diesel"),
        ("EA888", "2.0TSI EA888"),
        ("K9K", "1,5dCi K9K"),
        ("2.0TSI", "2.0TSI"),
        ("2.0 TSI", "2.0TSI"),
        ("diesel", "diesel"),
    ],
)
def test_existing_aliases_and_equal_descriptions_remain_compatible(route, left, right):
    result, status = compare(route, left, right)
    assert status != "disputed"
    assert result["vehicle_profile"].get("engine")
    assert not any(row["field"] == "engine" for row in result["conflicts"])
