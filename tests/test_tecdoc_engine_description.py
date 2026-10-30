"""Engine descriptions cannot hide an unresolved exact-engine constraint."""

import asyncio
import json
import socket

from mcp.server.fastmcp import FastMCP
import pytest

from autostop_manager import automotive_identity, mcp_telemetry, mcp_tools
from autostop_manager.tecdoc_vehicle_selection import resolve_tecdoc_vehicle
from tests.test_tecdoc_unverified_selection import _provider_identity
from tests.test_tecdoc_vehicle_selection import _car, _catalogue, _identity


@pytest.fixture(scope="module")
def native():
    class Inert:
        def __init__(self, *_args, **_kwargs):
            pass

    # Windows initializes its internal socket pair before network is forbidden.
    loop = asyncio.new_event_loop()
    try:
        with pytest.MonkeyPatch.context() as patch:
            for name in (
                "StoreIntegration",
                "StoreApiClient",
                "StoreOwnerApiClient",
                "StoreQuoteConductor",
                "StoreQuoteOwnerApi",
            ):
                patch.setattr(mcp_tools, name, Inert)
            for name in (
                "get_store_api_url",
                "get_store_read_token",
                "get_store_manage_token",
                "get_store_quote_token",
                "get_store_owner_token",
            ):
                patch.setattr(mcp_tools, name, lambda: "")
            patch.setattr(mcp_telemetry.LOGGER, "disabled", True)
            server = FastMCP("synthetic-tecdoc-engine-description")
            mcp_tools.register_manager_tools(
                server, store=Inert(), store_client=Inert(), include_tools={"compare_vehicle_modifications"}
            )
            yield server._tool_manager.get_tool("compare_vehicle_modifications"), loop
    finally:
        loop.close()


@pytest.fixture(autouse=True)
def forbidden_network(monkeypatch, native):
    def denied(*_args, **_kwargs):
        raise AssertionError("synthetic engine-description tests must not access the network")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)


@pytest.fixture(params=["direct", "native"])
def compare(request, native):
    def invoke(engine, candidates):
        arguments = {"context": {"engine": engine}, "candidates": candidates}
        if request.param == "direct":
            return automotive_identity.compare_vehicle_modifications(**arguments)
        tool, loop = native
        content, result = loop.run_until_complete(tool.run(arguments, convert_result=True))
        assert json.loads(content[0].text) == result
        return result

    return invoke


@pytest.mark.parametrize("description", ["3.0 TDI CJMA", "3.0 TDI unknown", "3.0 TDI DOHC"])
def test_unresolved_description_never_selects_the_single_candidate(compare, description):
    candidate = _car(displacement_cc=2967, fuel_type="diesel")
    result = compare(description, [candidate])
    assert result["data"]["selected"] is None
    assert result["outcome"] == "partial"
    assert result["missing_fields"] == ["engine"]
    assert result["data"]["alternatives"] == [candidate]
    assert result["conflicts"] == []  # Do not invent a code from arbitrary prose.


@pytest.mark.parametrize("description", ["3.0 TDI CJMA", "3.0 TDI unknown"])
def test_unresolved_description_keeps_engine_enrichment(description):
    callback, calls = _catalogue([_car(displacement_cc=2967, fuel_type="diesel")])
    result = resolve_tecdoc_vehicle(_identity(engine=description), vehicle_type="PC", call=callback)
    assert calls[-1][0] == "engine_info"
    assert result["status"] == "needs_vehicle_context"
    assert result["selected_profile"] is None
    assert "engine" in result["missing_fields"]
    assert "engine" not in result["matched_fields"]


@pytest.mark.parametrize(
    "description", ["3.0 TDI CRCA", "3.0 tdi (crca)", "3.0 TDI CR-CA", "CRCA 3.0 TDI", "3,0 TDI CRCA"]
)
def test_explicit_catalogue_code_confirms_compatible_description(compare, description):
    candidate = _car(displacement_cc=2967, fuel_type="diesel")
    result = compare(description, [candidate])
    assert result["data"]["selected"] == candidate
    assert result["missing_fields"] == []


def test_description_without_catalogue_code_requires_engine_info():
    callback, calls = _catalogue(
        [{"tecdoc_car_id": 101, "engine": "3.0 TDI", "displacement_cc": 2967}],
        engines=[{"engine_code": "CRCA"}],
    )
    result = resolve_tecdoc_vehicle(_identity(engine="3.0 TDI CRCA"), vehicle_type="PC", call=callback)
    assert calls[-1][0] == "engine_info"
    assert result["status"] == "matched"
    assert result["selected_profile"]["engine_code"] == "CRCA"


@pytest.mark.parametrize("engine", ["3.0 TDI", "CRCA", "cr-ca"])
def test_plain_description_and_exact_codes_keep_existing_behavior(compare, engine):
    candidate = _car(displacement_cc=2967, fuel_type="diesel")
    assert compare(engine, [candidate])["data"]["selected"] == candidate
    callback, calls = _catalogue([candidate])
    result = resolve_tecdoc_vehicle(_identity(engine=engine), vehicle_type="PC", call=callback)
    assert result["status"] == "matched"
    assert calls[-1][0] == "getCars"


@pytest.mark.parametrize("provenance", [{}, {"field_provenance": {"engine": "NHTSA vPIC"}}])
def test_unverified_unknown_description_cannot_be_hidden_by_supported_power(provenance):
    callback, calls = _catalogue([_car(displacement_cc=2967, power_hp=245, fuel_type="diesel")])
    result = resolve_tecdoc_vehicle(
        _provider_identity(power_hp=245),
        vehicle_type="PC",
        call=callback,
        independent_context={"engine": "3.0 TDI CJMA", **provenance},
        trusted_provider_evidence=True,
    )
    assert calls[-1][0] == "engine_info"
    assert result["status"] == "needs_vehicle_context"
    assert result["selected_profile"] is None
    assert "engine" in result["missing_fields"]


@pytest.mark.parametrize("facts", [{"displacement_cc": 1998}, {"fuel_type": "petrol"}])
def test_description_keeps_clear_technical_conflicts(compare, facts):
    result = compare("3.0 TDI CRCA", [_car(displacement_cc=2967, fuel_type="diesel") | facts])
    assert result["data"]["selected"] is None
    assert result["conflicts"][0]["field"] == "engine"


def test_exact_code_conflict_keeps_existing_behavior(compare):
    result = compare("CJMA", [_car(displacement_cc=2967, fuel_type="diesel")])
    assert result["data"]["selected"] is None
    assert result["conflicts"][0]["field"] == "engine"


def test_matching_and_unresolved_candidates_remain_alternatives(compare):
    candidates = [_car(displacement_cc=2967), _car(102, engine_code="CJMA", displacement_cc=2967)]
    result = compare("3.0 TDI CJMA", candidates)
    assert result["data"]["selected"] is None
    assert result["data"]["alternatives"] == candidates
    assert "engine" in result["missing_fields"]


@pytest.mark.parametrize("codes", ["CJMA, CRCA", "CJMA; CRCA", "CJMA/CRCA"])
def test_explicit_catalogue_code_alternatives_keep_alias_comparison(compare, codes):
    candidate = _car(engine_code=codes, displacement_cc=2967)
    assert compare("3.0 TDI CRCA", [candidate])["data"]["selected"] == candidate


@pytest.mark.parametrize("code", ["CJMA", "TDI", "PETROL"])
def test_explicit_code_remains_authoritative_over_engine_description(compare, code):
    candidate = _car(engine_code=code, engine="CRCA", displacement_cc=2967, fuel_type="diesel")
    result = compare("3.0 TDI CRCA", [candidate])
    assert result["data"]["selected"] is None
    assert "engine" in result["missing_fields"]


@pytest.mark.parametrize("description", ["3.0 TDI CRCA1", "3.0 TDI CRCA-T", "3.0 TDI CRCA or CJMA"])
def test_code_prefix_or_prose_is_not_an_alias(compare, description):
    result = compare(description, [_car(displacement_cc=2967)])
    assert result["data"]["selected"] is None
    assert result["conflicts"] == []
    assert result["missing_fields"] == ["engine"]


def test_repeated_marketing_text_does_not_become_an_exact_code(compare):
    candidate = {"tecdoc_car_id": 101, "engine": "3.0 TDI CRCA", "displacement_cc": 2967}
    assert compare("3.0 TDI CRCA", [candidate])["data"]["selected"] is None
    assert compare("3.0 TDI", [candidate])["data"]["selected"] == candidate


def test_confirmed_caller_alias_does_not_become_independent_engine_evidence():
    callback, calls = _catalogue([_car(displacement_cc=2967, power_hp=245, fuel_type="diesel")])
    result = resolve_tecdoc_vehicle(
        _provider_identity(power_hp=245),
        vehicle_type="PC",
        call=callback,
        independent_context={"engine": "3.0 TDI CRCA", "field_provenance": {"engine": "NHTSA vPIC"}},
        trusted_provider_evidence=True,
    )
    assert result["status"] == "matched"
    assert "engine" not in result["matched_fields"]
    assert "engine" in result["missing_fields"]
    assert calls[-1][0] == "getCars"
