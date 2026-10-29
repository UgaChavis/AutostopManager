"""Contradictory inclusion metadata must never become a complete zero quote."""

from __future__ import annotations

import asyncio
from copy import deepcopy
import json
import socket

from mcp.server.fastmcp import FastMCP
import pytest

from autostop_manager import automotive_labor as labor
from autostop_manager.automotive_contracts import MAX_ROWS
from autostop_manager.automotive_mcp import register_automotive_tools

POLICY = {"basis": "hourly_rate", "version": "inclusion-regression-v1", "currency": "RUB"}
CYCLES = [
    pytest.param([{"operation_id": "A", "hours": 2, "included_operations": ["A"]}], id="self"),
    pytest.param(
        [
            {"operation_id": "A", "hours": 2, "included_operations": ["B"]},
            {"operation_id": "B", "hours": 1, "included_operations": ["A"]},
        ],
        id="two-node-cycle",
    ),
    pytest.param(
        [
            {"operation_id": "A", "hours": 2, "included_operations": ["B"]},
            {"operation_id": "B", "hours": 1, "included_operations": ["C"]},
            {"operation_id": "C", "hours": 1, "included_operations": ["A"]},
            {"operation_id": "unrelated", "hours": 3},
        ],
        id="cycle-with-an-unrelated-price",
    ),
    pytest.param(
        [
            {"operation_id": "A", "hours": 2},
            {"operation_id": "A", "hours": 1, "included_operations": ["A"]},
        ],
        id="self-in-later-duplicate",
    ),
    pytest.param(
        [
            {"operation_id": "A", "hours": 2},
            {"operation_id": "B", "hours": 1, "included_operations": ["A"]},
            {"operation_id": "A", "hours": 5, "included_operations": ["B"]},
        ],
        id="cycle-in-later-duplicate",
    ),
    pytest.param([{"operation_id": 0, "hours": 2, "included_operations": ["0"]}], id="canonical-zero-id"),
    pytest.param([{"operation_name": "A", "hours": 2, "included_operations": ["A"]}], id="name-key-self"),
    pytest.param([{"hours": 2, "included_operations": [0]}], id="index-key-self"),
]


@pytest.fixture(autouse=True)
def pure_calculation_only(monkeypatch, tmp_path):
    def denied(*_args, **_kwargs):
        raise AssertionError("pricing validation attempted external acquisition")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)
    monkeypatch.setattr(labor, "collect_work_price_evidence", denied)
    monkeypatch.setenv("AUTOSTOP_MANAGER_ENV_FILE", "/dev/null")
    monkeypatch.setenv("AUTOSTOP_MANAGER_DB", str(tmp_path / "synthetic.sqlite3"))


@pytest.fixture
def native_tool():
    server = FastMCP("inclusion-cycle-regression")
    register_automotive_tools(server)
    return server._tool_manager.get_tool("calculate_work_price")


def assert_invalid_cycle(response):
    assert response["ok"] is False
    assert response["outcome"] == "invalid_input"
    assert response["data"] == {}
    assert response["missing_fields"] == ["labor.included_operations"]
    assert response["execution"] == {"network_calls": 0, "attempts": []}


@pytest.mark.parametrize("rows", CYCLES)
def test_cyclic_inclusion_is_invalid_instead_of_a_complete_zero_price(rows):
    original = deepcopy(rows)
    assert_invalid_cycle(labor.calculate_work_price(rows, deepcopy(POLICY), hourly_rate=2000))
    assert rows == original


@pytest.mark.parametrize("rows", CYCLES)
@pytest.mark.parametrize("encoded", [False, True])
def test_native_calculator_rejects_cycles_before_returning_a_price(native_tool, rows, encoded):
    arguments = {"labor": deepcopy(rows), "policy": deepcopy(POLICY), "hourly_rate": 2000}
    original = deepcopy(arguments)
    native_tool.fn_metadata.arg_model.model_validate_json(json.dumps(arguments))
    wire = {key: json.dumps(value) if isinstance(value, (dict, list)) else value for key, value in arguments.items()}
    content, response = asyncio.run(native_tool.run(wire if encoded else arguments, convert_result=True))
    assert json.loads(content[0].text) == response
    assert_invalid_cycle(response)
    assert arguments == original


def test_transitive_package_and_later_duplicate_relations_keep_existing_deduplication():
    rows = [
        {"operation_id": "A", "hours": 2, "included_operations": ["B", "B", "external"]},
        {"operation_id": "B", "hours": 1, "included_operations": [0]},
        {"operation_id": 0, "hours": 1},
        {"operation_id": "A", "hours": 9, "included_operations": ["C"]},
        {"operation_id": "C", "hours": 1},
        {"operation_id": "D", "hours": 0.25},
    ]
    original = deepcopy(rows)
    response = labor.calculate_work_price(rows, deepcopy(POLICY), hourly_rate=2000)
    assert response["outcome"] == "success"
    assert response["data"]["total"] == 4500
    assert [row["operation_id"] for row in response["data"]["operations"]] == ["A", "D"]
    assert [row["operation_id"] for row in response["data"]["exclusions"]] == ["B", "0", "A", "C"]
    assert rows == original


def test_unpriced_parent_keeps_included_child_unknown_instead_of_claiming_a_zero_total():
    response = labor.calculate_work_price(
        [{"operation_id": "A", "included_operations": ["B"]}, {"operation_id": "B", "hours": 1}],
        deepcopy(POLICY),
        hourly_rate=2000,
    )
    assert response["outcome"] == "partial"
    assert response["data"]["total"] is None and response["data"]["range"] is None
    assert response["missing_fields"] == ["A.price_basis"]


def test_maximum_size_inclusion_chain_is_bounded_and_uses_no_recursion():
    rows = [
        {"operation_id": index, "hours": 2, "included_operations": [index + 1] if index + 1 < MAX_ROWS else []}
        for index in range(MAX_ROWS)
    ]
    response = labor.calculate_work_price(rows, deepcopy(POLICY), hourly_rate=2000)
    assert response["outcome"] == "success" and response["data"]["total"] == 4000
    rows[-1]["included_operations"] = [0]
    assert_invalid_cycle(labor.calculate_work_price(rows, deepcopy(POLICY), hourly_rate=2000))


@pytest.mark.parametrize("hours,total", [(0, 0), (0.29, 15)])
def test_legitimate_zero_and_half_up_rounding_are_unchanged(hours, total):
    response = labor.calculate_work_price(
        [{"operation_id": "A", "hours": hours}], {**POLICY, "rounding": 1}, hourly_rate=50
    )
    assert response["outcome"] == "success"
    assert response["data"]["total"] == total
