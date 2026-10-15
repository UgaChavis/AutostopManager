"""Malformed policy bases produce the public invalid-input envelope."""

from __future__ import annotations

import asyncio
from copy import deepcopy
import json
import socket

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
import pytest

from autostop_manager import automotive_labor as labor, work_pricing as pricing, work_pricing_research as research
from autostop_manager.automotive_mcp import register_automotive_tools


PUBLIC_POLICY = {"basis": "public_observations", "version": "policy-test-v1"}
INVALID_BASES = [
    pytest.param(None, id="null"),
    pytest.param(False, id="false"),
    pytest.param(True, id="true"),
    pytest.param(0, id="zero"),
    pytest.param(1, id="integer"),
    pytest.param(1.5, id="float"),
    pytest.param("", id="empty-string"),
    pytest.param("HOURLY_RATE", id="uppercase"),
    pytest.param(" hourly_rate", id="leading-space"),
    pytest.param("public_observations ", id="trailing-space"),
    pytest.param([], id="empty-list"),
    pytest.param(["hourly_rate"], id="list"),
    pytest.param({}, id="empty-object"),
    pytest.param({"basis": "hourly_rate"}, id="object"),
]


@pytest.fixture(autouse=True)
def isolated_calculation(monkeypatch, tmp_path):
    def denied(*_args, **_kwargs):
        raise AssertionError("policy calculation attempted network or private acquisition")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)
    monkeypatch.setattr(pricing, "_load_labor_experience", denied)
    monkeypatch.setattr(pricing, "collect_public_work_pricing_research", denied)
    monkeypatch.setattr(research, "collect_public_work_pricing_research", denied)
    monkeypatch.setattr(research, "urlopen", denied)
    monkeypatch.setattr(labor, "collect_work_price_evidence", denied)
    monkeypatch.setenv("AUTOSTOP_MANAGER_ENV_FILE", "/dev/null")
    monkeypatch.setenv("AUTOSTOP_MANAGER_DB", str(tmp_path / "manager.sqlite3"))
    monkeypatch.setenv("AUTOSTOP_J1_CACHE_DIR", str(tmp_path / "j1"))
    monkeypatch.setenv("AUTOSTOP_J1_VIN_CACHE_DIR", str(tmp_path / "vin"))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))


def quote(name="demo", price=2000):
    return {"operation_name": name, "price_rub": price, "labor_only": True, "includes_parts": False}


def call(arguments):
    snapshot = deepcopy(arguments)
    response = labor.calculate_work_price(**arguments)
    assert arguments == snapshot
    assert response["execution"] == {"network_calls": 0, "attempts": []}
    return response


def assert_invalid(response, field="policy_version_basis_rate"):
    assert response["schema_version"] == "autostop.automotive-result.v1"
    assert response["tool_id"] == "manager.calculate_work_price"
    assert response["ok"] is False and response["outcome"] == "invalid_input"
    assert response["data"] == {} and response["missing_fields"] == [field]
    assert response["execution"] == {"network_calls": 0, "attempts": []}


@pytest.mark.parametrize("basis", INVALID_BASES)
def test_invalid_basis_is_a_global_validation_error_not_a_partial_price(basis):
    response = call(
        {
            "labor": [{"operation_name": "demo", "hours": 2}],
            "policy": {**PUBLIC_POLICY, "basis": basis},
            "hourly_rate": 1000,
            "observations": [quote()],
        }
    )
    assert_invalid(response)


def test_missing_basis_has_the_same_public_validation_error():
    assert_invalid(call({"labor": [], "policy": {"version": "policy-test-v1"}}))


@pytest.fixture
def native_tool():
    server = FastMCP("isolated-price-policy")
    register_automotive_tools(server)
    return server._tool_manager.get_tool("calculate_work_price")


@pytest.mark.parametrize("basis", INVALID_BASES)
@pytest.mark.parametrize("encoded", [False, True])
def test_registered_native_tool_returns_invalid_basis_in_both_result_channels(native_tool, basis, encoded):
    arguments = {
        "labor": [{"operation_name": "demo"}],
        "policy": {**PUBLIC_POLICY, "basis": basis},
        "observations": [quote()],
    }
    snapshot = deepcopy(arguments)
    model = native_tool.fn_metadata.arg_model.model_validate_json(json.dumps(arguments))
    assert model.model_dump()["policy"] == arguments["policy"]
    wire = {key: json.dumps(value) for key, value in arguments.items()} if encoded else arguments
    content, response = asyncio.run(native_tool.run(wire, convert_result=True))
    assert json.loads(content[0].text) == response
    assert_invalid(response)
    assert arguments == snapshot


@pytest.mark.parametrize("basis", [[], {}])
@pytest.mark.parametrize("version", [{}, {"version": None}, {"version": ""}, {"version": 0}])
def test_missing_or_false_version_keeps_existing_short_circuit(basis, version):
    assert_invalid(call({"labor": [], "policy": {"basis": basis, **version}}))


@pytest.mark.parametrize(
    "rows,policy",
    [(None, {"version": "v1", "basis": []}), (["not-a-row"], {"version": "v1", "basis": {}}), ([], None), ([], [])],
)
def test_outer_labor_and_policy_validation_precedes_nested_basis(rows, policy):
    assert_invalid(call({"labor": rows, "policy": policy}), "labor_or_policy")


@pytest.mark.parametrize("conflict", ["coefficient", "rounding", "rate", "observations", "row-and-unknown-costs"])
def test_invalid_basis_precedes_later_validation_errors(conflict):
    arguments = {
        "labor": [{"operation_id": "demo", "operation_name": "demo"}],
        "policy": {**PUBLIC_POLICY, "basis": ["public_observations"]},
    }
    if conflict in {"coefficient", "rounding"}:
        arguments["policy"][conflict] = 0
    elif conflict == "rate":
        arguments["hourly_rate"] = "not-a-rate"
    elif conflict == "observations":
        arguments["observations"] = ["not-an-observation"]
    else:
        arguments["labor"][0]["operation_id"] = True
        arguments["unknown_costs"] = [False]
    assert_invalid(call(arguments))
    arguments["policy"]["basis"] = "hourly_rate" if conflict == "rate" else "public_observations"
    expected = {"observations": "observations", "row-and-unknown-costs": "labor.structure"}.get(
        conflict, "policy_version_basis_rate"
    )
    assert_invalid(call(arguments), expected)


@pytest.mark.parametrize(
    "conflict,field", [("observations", "observations"), ("row", "labor.structure"), ("unknown-costs", "unknown_costs")]
)
def test_valid_basis_keeps_the_later_validation_error_contract(conflict, field):
    arguments = {"labor": [{"operation_id": "demo", "operation_name": "demo"}], "policy": deepcopy(PUBLIC_POLICY)}
    if conflict == "observations":
        arguments["observations"] = ["not-an-observation"]
    elif conflict == "row":
        arguments["labor"][0]["operation_id"] = True
    else:
        arguments["unknown_costs"] = [False]
    assert_invalid(call(arguments), field)


@pytest.mark.parametrize(
    "fields,total,bounds",
    [({"hours": 0}, 0, [0, 0]), ({"hours": 2}, 2000, [2000, 2000]), ({"range_hours": [1, 2]}, 1500, [1000, 2000])],
)
def test_hourly_basis_preserves_defaults_zero_and_ranges(fields, total, bounds):
    policy = {"basis": "hourly_rate", "version": "policy-test-v1"}
    response = call({"labor": [{"operation_id": "hourly", **fields}], "policy": policy, "hourly_rate": 1000})
    assert response["outcome"] == "success"
    assert response["data"]["total"] == total and response["data"]["range"] == bounds
    assert response["data"]["policy"] == policy and response["data"]["currency"] == "RUB"


@pytest.mark.parametrize(
    "prices,options,total,bounds",
    [
        ([0], {}, 0, [0, 0]),
        ([1000, 2000, 4000], {"coefficient": 1.25, "rounding": 100}, 2500, [1300, 5000]),
        ([1000, 3000], {}, 2000, [1000, 3000]),
    ],
)
def test_public_basis_preserves_zero_median_and_rounding(prices, options, total, bounds):
    policy = {**PUBLIC_POLICY, **options}
    response = call(
        {
            "labor": [{"operation_name": "demo"}],
            "policy": policy,
            "observations": [quote(price=price) for price in prices],
        }
    )
    assert response["outcome"] == "success"
    assert response["data"]["total"] == total and response["data"]["range"] == bounds
    assert response["data"]["policy"] == policy


def test_valid_public_policy_preserves_known_subtotal_and_unknown_costs():
    response = call(
        {
            "labor": [
                {"operation_id": "known", "operation_name": "demo"},
                {"operation_id": "unknown", "operation_name": "unpriced work"},
            ],
            "policy": deepcopy(PUBLIC_POLICY),
            "observations": [quote()],
            "unknown_costs": ["extra-work"],
        }
    )
    assert response["ok"] is True and response["outcome"] == "partial"
    assert response["data"]["known_subtotal"] == 2000
    assert response["data"]["total"] is None and response["data"]["range"] is None
    assert response["missing_fields"] == ["extra-work", "unknown.price_basis"]
    assert response["data"]["unknown_costs"] == ["extra-work", "unknown.price_basis"]
    assert response["data"]["exclusions"] == [{"operation_id": "unknown", "reason": "unknown_labor_or_price"}]


@pytest.mark.parametrize("name", [None, "\u00a0\t", ["demo"]])
def test_valid_policy_keeps_unidentified_public_work_partial(name):
    response = call(
        {
            "labor": [{"operation_id": "unknown", "operation_name": name}],
            "policy": deepcopy(PUBLIC_POLICY),
            "observations": [quote(name)],
        }
    )
    assert response["outcome"] == "partial" and response["ok"] is True
    assert response["data"]["known_subtotal"] == 0
    assert response["data"]["total"] is None and response["data"]["range"] is None
    assert response["missing_fields"] == ["unknown.price_basis"]


@pytest.mark.parametrize("basis,total", [("hourly_rate", 0), ("public_observations", 2000)])
@pytest.mark.parametrize("encoded", [False, True])
def test_registered_native_tool_keeps_valid_basis_results(native_tool, basis, total, encoded):
    arguments = {
        "labor": [{"operation_id": "demo", "operation_name": "demo", "hours": 0}],
        "policy": {"basis": basis, "version": "policy-test-v1"},
        "hourly_rate": 1000,
        "observations": [quote()],
    }
    snapshot = deepcopy(arguments)
    wire = (
        {key: json.dumps(value) if isinstance(value, (dict, list)) else value for key, value in arguments.items()}
        if encoded
        else arguments
    )
    content, response = asyncio.run(native_tool.run(wire, convert_result=True))
    assert json.loads(content[0].text) == response
    assert response["outcome"] == "success" and response["data"]["total"] == total
    assert response["execution"]["network_calls"] == 0 and arguments == snapshot


@pytest.mark.parametrize("policy", [None, []])
def test_native_wrong_outer_policy_type_remains_an_sdk_schema_error(native_tool, policy):
    with pytest.raises(ToolError, match="policy"):
        asyncio.run(native_tool.run({"labor": [], "policy": policy}))
