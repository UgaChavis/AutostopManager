"""Ready numeric evidence cannot turn an exponent into its mantissa."""

import asyncio
from copy import deepcopy
import json
import socket

from mcp.server.fastmcp import FastMCP
import pytest

from autostop_manager import mcp_read_executor, mcp_telemetry, mcp_tools, work_pricing as pricing
from autostop_manager.listing_executor import BoundedListingExecutor


OPERATION = "замена масла"


@pytest.fixture(autouse=True)
def forbid_acquisition(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("ready numeric evidence must not acquire private or public data")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)
    monkeypatch.setattr(pricing, "get_db_path", denied)
    monkeypatch.setattr(pricing, "_load_labor_experience", denied)
    monkeypatch.setattr(pricing, "collect_public_work_pricing_research", denied)


@pytest.fixture(scope="module")
def native_tool():
    class Inert:
        def __init__(self, *args, **kwargs):
            pass

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
        pool = BoundedListingExecutor(max_workers=1, max_waiting_calls=0, timeout_seconds=2)
        patch.setattr(mcp_read_executor, "_WEB_EXECUTOR", pool)
        server = FastMCP("isolated-ready-exponent-literals")
        try:
            mcp_tools.register_manager_tools(
                server, store=Inert(), store_client=Inert(), include_tools={"estimate_repair_work_cost"}
            )
            yield server._tool_manager.get_tool("estimate_repair_work_cost")
        finally:
            pool.close()


@pytest.fixture(params=["direct", "native", "native-json"])
def estimate(request, native_tool):
    def call(kind, value, *, field=None):
        field = field or ("hours" if kind == "hours" else "price_rub")
        rows = [
            {
                "source": f"synthetic-{index}.example",
                "operation_name": OPERATION,
                "city": "Красноярск",
                field: value,
                "confidence": "high",
                **({"public_source": True} if kind == "hours" else {"labor_only": True, "includes_parts": False}),
            }
            for index in range(2 if kind == "hours" else 3)
        ]
        arguments = {
            "make": "DEMO",
            "work_items": [OPERATION],
            "price_evidence": {
                "work_items": [OPERATION],
                "vehicle_context": {"make": "DEMO"},
                "observations": rows if kind == "price" else [],
                "labor": rows if kind == "hours" else [],
            },
            "auto_research": False,
            "use_internal_experience": False,
        }
        snapshot = deepcopy(arguments)
        if request.param == "direct":
            response = pricing.estimate_repair_work_cost(**arguments)
        else:
            wire = {
                key: json.dumps(value) if request.param == "native-json" and isinstance(value, (dict, list)) else value
                for key, value in arguments.items()
            }
            content, response = asyncio.run(native_tool.run(wire, convert_result=True))
            assert json.loads(content[0].text) == response
        assert response["ok"] is True
        assert arguments == snapshot
        return response

    return call


@pytest.mark.parametrize("kind", ["hours", "price"])
@pytest.mark.parametrize("value", ["1e309", "1e-999", "-1e1", "−1e1", "1e+", "1e++3", "1e--3", "1e3.5", "1ee3"])
def test_invalid_exponent_cannot_create_positive_price_or_hours(estimate, kind, value):
    result = estimate(kind, value)
    operation = result["operation_estimates"][0]

    if kind == "hours":
        analysis = operation["labor_time_analysis"]
        assert analysis["valid_count"] == 0
        assert analysis["average_hours"] is None
    else:
        assert operation["sample"]["valid_count"] == 0
        assert operation["market_average_rub"] is None
    assert operation["autostop_price_rub"] is None
    assert result["recommended_total_works_rub"] is None
    assert result["total_works_rub"] is None


@pytest.mark.parametrize("value", ["1e3", "1e-3", "1e309–3", "1–3e309", "1e+–3"])
def test_invalid_hours_atom_or_range_cannot_salvage_an_endpoint(estimate, value):
    result = estimate("hours", value)
    analysis = result["operation_estimates"][0]["labor_time_analysis"]

    assert analysis["valid_count"] == 0
    assert analysis["average_hours"] is None
    assert result["total_works_rub"] is None


@pytest.mark.parametrize(
    "kind,scientific,numeric",
    [
        ("hours", "1e-1", 0.1),
        ("hours", "1e1", 10),
        ("hours", "8E+1 часа", 80),
        ("hours", "Норма 2,5e0 часа", 2.5),
        ("price", "1e3", 1000),
        ("price", "2.5E+3 руб.", 2500),
        ("price", "+1e4", 10000),
    ],
)
def test_finite_scientific_values_match_numeric_evidence(estimate, kind, scientific, numeric):
    actual = estimate(kind, scientific)
    expected = estimate(kind, numeric)
    actual_operation, expected_operation = actual["operation_estimates"][0], expected["operation_estimates"][0]

    if kind == "hours":
        for key in ("valid_count", "average_hours", "range_hours"):
            assert actual_operation["labor_time_analysis"][key] == expected_operation["labor_time_analysis"][key]
    else:
        assert actual_operation["sample"]["valid_count"] == expected_operation["sample"]["valid_count"]
        assert actual_operation["market_average_rub"] == expected_operation["market_average_rub"]
    assert actual["total_works_rub"] == expected["total_works_rub"]


@pytest.mark.parametrize(
    "kind,text,numeric",
    [
        ("hours", "2,5 часа", 2.5),
        ("hours", "Норма 1 - 3 часа", [1, 3]),
        ("hours", "2,5–3,5 часа", [2.5, 3.5]),
        ("price", "5 000 руб.", 5000),
        ("price", "+ 5000 ₽", 5000),
        ("price", "1500,5 руб.", 1500.5),
    ],
)
def test_existing_prose_decimal_grouping_and_ranges_remain_compatible(estimate, kind, text, numeric):
    actual, expected = estimate(kind, text), estimate(kind, numeric)

    assert actual["total_works_rub"] == expected["total_works_rub"]
    assert actual["recommended_total_works_rub"] == expected["recommended_total_works_rub"]


@pytest.mark.parametrize(
    "kind,field", [("hours", "labor_hours"), ("hours", "norm_hours"), ("hours", "time_hours"), ("price", "price")]
)
def test_existing_numeric_field_aliases_use_complete_exponent_value(estimate, kind, field):
    result = estimate(kind, "1e309", field=field)

    assert result["operation_estimates"][0]["autostop_price_rub"] is None
    assert result["total_works_rub"] is None
