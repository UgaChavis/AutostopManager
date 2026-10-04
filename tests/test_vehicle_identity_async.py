from __future__ import annotations

import asyncio
import json
import socket

import httpx
import pytest

from autostop_manager import vehicle_identity_async as driver
from autostop_manager import vehicle_identity_transport as transport


def synthetic_vin(index=1):
    return "1HG" + "CM8263" + f"{index:08d}"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.setattr(transport, "_PROVIDER_CIRCUIT", transport._ProviderCircuit())

    def forbidden(*args, **kwargs):
        raise AssertionError("unexpected real network")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


def test_async_batch_preserves_positions_and_common_invalid_row_shape():
    items = [
        {"identifier": "MR41S" + "-" + "123456", "make": "Suzuki"},
        {"identifier": synthetic_vin(), "crm_context": 42},
        {"identifier": synthetic_vin(2), "model_year": True},
        {"identifier": synthetic_vin(3), "crm_context": {"options": ["ABS"]}},
    ]
    result = asyncio.run(driver.decode_vehicle_identities_async(items, live_vpic=False))
    assert result["count"] == len(items)
    assert [row["item_index"] for row in result["results"]] == list(range(len(items)))
    assert result["results"][0]["ok"] is True
    for index in (1, 2):
        row = result["results"][index]
        assert row["ok"] is False
        assert row["status"] == "invalid_input"
        assert row["vehicle_profile"] == {}
        assert row["confidence_label"] == "low"
        assert row["parts_lookup_readiness"]["ready_for_oem_lookup"] is False
        assert row["errors"]
    assert result["processing"]["http_attempts"] == 0
    assert result["results"][3]["parts_lookup_readiness"]["ready_for_crm_writeback"] is False


@pytest.mark.parametrize("year", [True, False, 0, 1.5, {}, [], float("inf")])
def test_async_single_strict_year_validation_has_no_http(year):
    result = asyncio.run(driver.decode_vehicle_identity_async(synthetic_vin(), model_year=year))
    assert result["ok"] is False
    assert result["status"] == "invalid_input"


def test_async_single_preserves_compatibility_normalization_notes():
    result = asyncio.run(
        driver.decode_vehicle_identity_async(synthetic_vin(), model_year="2010", live_vpic=False, live_wmi=False)
    )
    assert result["vehicle_profile"]["model_year"] == 2010
    assert result["normalization_notes"] == [{"code": "year_string_normalized", "field": "model_year"}]


def test_async_empty_batch_and_max_input_guard():
    empty = asyncio.run(driver.decode_vehicle_identities_async([], live_vpic=False))
    assert empty["count"] == 0
    assert empty["results"] == []
    oversized = asyncio.run(driver.decode_vehicle_identities_async([{}] * 501))
    assert oversized["ok"] is False
    assert oversized["status"] == "invalid_input"
    assert oversized["errors"][0]["code"] == "identity_batch_too_large"


def test_async_builder_exception_isolated_to_its_input_position(monkeypatch):
    original = driver.decode_vehicle_identity

    def builder(identifier, **kwargs):
        if identifier.endswith("00000002"):
            raise ValueError("do not disclose identifiers or full exception")
        return original(identifier, **kwargs)

    monkeypatch.setattr(driver, "decode_vehicle_identity", builder)
    result = asyncio.run(
        driver.decode_vehicle_identities_async(
            [
                {"identifier": synthetic_vin(1)},
                {"identifier": synthetic_vin(2)},
                {"identifier": synthetic_vin(3)},
            ],
            live_vpic=False,
        )
    )
    assert result["count"] == 3
    assert [row["ok"] for row in result["results"]] == [True, False, True]
    assert result["results"][1]["errors"][0]["code"] == "identity_processing_error"
    assert "do not disclose" not in json.dumps(result)


def test_async_foreign_source_facts_do_not_enter_profile_or_evidence():
    vin = synthetic_vin()

    async def run():
        def handler(request):
            if "DecodeWMI" in request.url.path:
                return httpx.Response(200, json={"Results": []})
            return httpx.Response(
                200, json={"Results": [{"VIN": synthetic_vin(2), "Make": "Foreign", "Model": "Foreign"}]}
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await driver.decode_vehicle_identity_async(vin, client=client)

    result = asyncio.run(run())
    assert result["processing"]["http_attempts"] <= 2
    assert result["processing"]["partial"] is True
    assert "Foreign" not in json.dumps(result)
    assert result["parts_lookup_readiness"]["ready_for_crm_writeback"] is False


def test_async_normal_batch_cannot_trigger_hidden_sync_provider_request(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("pure merge attempted urllib request")

    monkeypatch.setattr("autostop_manager.vin_lookup.urlopen", forbidden)

    async def run():
        def handler(request):
            vin = synthetic_vin()
            return httpx.Response(200, json={"Results": [{"VIN": vin, "Make": "Honda", "Model": "Accord"}]})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await driver.decode_vehicle_identities_async([{"identifier": synthetic_vin()}], client=client)

    result = asyncio.run(run())
    assert result["count"] == 1
    assert result["results"][0]["ok"] is True
    assert result["processing"]["http_attempts"] == 1
