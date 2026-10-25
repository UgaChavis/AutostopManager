"""The pure calculator rounds decimal labor amounts, including exact half steps."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from decimal import getcontext, localcontext
import json
import socket
import sqlite3
import subprocess
import threading

from mcp.server.fastmcp import FastMCP
import pytest

from autostop_manager import automotive_labor as labor, work_pricing
from autostop_manager.automotive_mcp import register_automotive_tools


@pytest.fixture(autouse=True)
def pure_calculation(monkeypatch):
    def denied(*_args, **_kwargs):
        raise AssertionError("pure decimal calculation attempted external acquisition")

    for owner, name in (
        (socket.socket, "connect"),
        (socket.socket, "connect_ex"),
        (socket, "create_connection"),
        (socket, "getaddrinfo"),
        (sqlite3, "connect"),
        (subprocess, "Popen"),
        (threading.Thread, "start"),
        (work_pricing, "_load_labor_experience"),
        (work_pricing, "get_db_path"),
        (labor, "collect_work_price_evidence"),
    ):
        monkeypatch.setattr(owner, name, denied)


def calculate(rows, *, rate=1, basis="hourly_rate", observations=None, unknown_costs=None, **options):
    arguments = {
        "labor": rows,
        "hourly_rate": rate,
        "policy": {"basis": basis, "version": "decimal-test-v1", **options},
        "observations": observations,
        "unknown_costs": unknown_costs,
    }
    original = deepcopy(arguments)
    response = labor.calculate_work_price(**arguments)
    assert arguments == original
    assert response["execution"] == {"network_calls": 0, "attempts": []}
    json.dumps(response, allow_nan=False)
    return response


@pytest.mark.parametrize(
    "hours,rate,coefficient,quantum,expected",
    [
        (0.29, 50, 1, 1, 15),
        (1, 14.5, 1, 1, 15),
        (0.58, 25, 1, 1, 15),
        (0.29, 100, 0.5, 1, 15),
        (1, 50, 0.29, 1, 15),
        (0.29, 5, 1, 0.1, 1.5),
        (0.29, 50, 1, 0.2, 14.6),
        (0.291, 50, 1, 0.3, 14.7),
        (0.29, 0.0005, 1, 0.00001, 0.00015),
        (0.2899999999999999, 50, 1, 1, 14),
        (0.2900000000000001, 50, 1, 1, 15),
        (0.2899999999999999, 5, 1, 0.1, 1.4),
        ("0,29", "50", "1", "1", 15),
        (0, 50, 0.29, 0.1, 0),
    ],
)
def test_hourly_half_steps_depend_on_decimal_amount_not_factorization(hours, rate, coefficient, quantum, expected):
    response = calculate([{"hours": hours}], rate=rate, coefficient=coefficient, rounding=quantum)
    assert response["outcome"] == "success"
    data = response["data"]
    assert data["total"] == data["known_subtotal"] == expected
    assert data["range"] == [expected, expected]
    assert data["operations"][0]["price"] == expected
    assert data["operations"][0]["range"] == [expected, expected]


@pytest.mark.parametrize(
    "bounds,quantum,expected,expected_range",
    [([0.27, 0.29], 1, 14, [14, 15]), ([0, 0.57], 0.5, 14.5, [0, 28.5])],
)
def test_hourly_range_midpoint_and_bounds_use_the_same_decimal_rule(bounds, quantum, expected, expected_range):
    response = calculate([{"range_hours": bounds}], rate=50, rounding=quantum)
    assert response["outcome"] == "success"
    assert response["data"]["total"] == expected
    assert response["data"]["range"] == expected_range


@pytest.mark.parametrize(
    "hour_bounds,rate,outcome,subtotal,total_range",
    [
        ([1.5000000000000007, 1.5000000000000004], 1.5, "partial", 3, None),
        ([1.5000000000000004, 1.5000000000000007], 1.5, "success", 8, [7, 8]),
        ([1.5000000000000007, 1.5000000000000004], 0, "success", 0, [0, 0]),
    ],
)
def test_decimal_bounds_cannot_reverse_after_equal_binary_products(hour_bounds, rate, outcome, subtotal, total_range):
    response = calculate(
        [{"operation_id": "range", "hours": 1, "range_hours": hour_bounds}, {"operation_id": "known", "hours": 1}],
        rate=rate,
        coefficient=1.9999999999999993,
    )
    assert response["outcome"] == outcome
    assert response["data"]["known_subtotal"] == subtotal
    assert response["data"]["total"] == (None if outcome == "partial" else subtotal)
    assert response["data"]["range"] == total_range
    if outcome == "partial":
        assert response["missing_fields"] == ["range.price_basis"]
        assert response["data"]["exclusions"] == [{"operation_id": "range", "reason": "unknown_labor_or_price"}]


@pytest.mark.parametrize(
    "sample,quantum,expected,expected_range",
    [([0.29], 1, 15, [15, 15]), ([0, 0.57], 0.5, 14.5, [0, 28.5])],
)
def test_public_sample_median_and_coefficient_keep_decimal_half_steps(sample, quantum, expected, expected_range):
    observations = [
        {"operation_name": "DEMO", "price_rub": value, "labor_only": True, "includes_parts": False} for value in sample
    ]
    response = calculate(
        [{"operation_name": "DEMO"}],
        basis="public_observations",
        observations=observations,
        coefficient=50,
        rounding=quantum,
    )
    assert response["outcome"] == "success"
    assert response["data"]["total"] == expected
    assert response["data"]["range"] == expected_range


@pytest.mark.parametrize("hours,expected", [([0.1, 0.2], 0.3), ([0.01] * 500, 5)])
def test_subtotal_and_range_sum_already_rounded_decimal_operations(hours, expected):
    response = calculate(
        [{"operation_id": str(index), "hours": value} for index, value in enumerate(hours)], rounding=0.01
    )
    assert response["outcome"] == "success"
    assert response["data"]["total"] == response["data"]["known_subtotal"] == expected
    assert response["data"]["range"] == [expected, expected]


def test_unknown_expense_keeps_total_unknown_after_corrected_known_subtotal():
    response = calculate([{"hours": 0.29}], rate=50, unknown_costs=["diagnostics"])
    assert response["outcome"] == "partial"
    assert response["data"]["known_subtotal"] == 15
    assert response["data"]["total"] is None and response["data"]["range"] is None
    assert response["data"]["unknown_costs"] == response["missing_fields"] == ["diagnostics"]


def test_total_sums_rounded_operations_instead_of_rounding_the_grand_total():
    response = calculate([{"operation_id": "a", "hours": 0.29}, {"operation_id": "b", "hours": 0.29}], rate=50)
    assert [row["price"] for row in response["data"]["operations"]] == [15, 15]
    assert response["data"]["total"] == 30
    assert response["data"]["range"] == [30, 30]


def test_rounding_precision_is_local_and_covers_large_and_small_terms():
    with localcontext() as context:
        context.prec = 6
        response = calculate([{"hours": 1e30}, {"hours": 0.29}], rate=50)
        half_step = calculate([{"hours": 0.29}], rate=50)
        assert getcontext().prec == 6
    assert response["outcome"] == "success"
    assert response["data"]["total"] == 5e31
    assert half_step["data"]["total"] == 15


@pytest.mark.parametrize("fields", [{"hours": 1e308}, {"range_hours": [1e308, 1e308]}])
def test_unrepresentable_hourly_amount_stays_invalid_before_rescaling(fields):
    response = calculate([fields], rate=2, coefficient=0.5)
    assert response["outcome"] == "invalid_input" and response["ok"] is False
    assert response["data"] == {} and response["missing_fields"] == ["calculation_overflow"]


@pytest.mark.parametrize("encoded", [False, True])
def test_native_result_channels_publish_only_finite_json_numbers(encoded):
    server = FastMCP("decimal-price-test")
    register_automotive_tools(server)
    arguments = {
        "labor": [{"hours": 0.29}],
        "policy": {"basis": "hourly_rate", "version": "decimal-test-v1"},
        "hourly_rate": 50,
    }
    original = deepcopy(arguments)
    wire = (
        {key: json.dumps(value) if isinstance(value, (list, dict)) else value for key, value in arguments.items()}
        if encoded
        else arguments
    )
    tool = server._tool_manager.get_tool("calculate_work_price")
    content, response = asyncio.run(tool.run(wire, convert_result=True))
    assert json.loads(content[0].text) == response
    assert response["outcome"] == "success" and response["data"]["total"] == 15
    assert response["execution"] == {"network_calls": 0, "attempts": []}
    assert arguments == original
    json.dumps(response, allow_nan=False)
