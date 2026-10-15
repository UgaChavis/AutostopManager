"""Public observation names must identify work before they can supply a price."""

from __future__ import annotations

import asyncio
from copy import deepcopy
import json
import socket

from mcp.server.fastmcp import FastMCP
import pytest

from autostop_manager import automotive_labor as labor, work_pricing as pricing, work_pricing_research as research
from autostop_manager.automotive_mcp import register_automotive_tools


POLICY = {"basis": "public_observations", "version": "public-name-test-v1"}
INVALID_NAMES = [
    pytest.param({}, id="missing"),
    pytest.param({"operation_name": None}, id="null"),
    pytest.param({"operation_name": ""}, id="empty"),
    pytest.param({"operation_name": " "}, id="space"),
    pytest.param({"operation_name": "\t\r\n"}, id="ascii-whitespace"),
    pytest.param({"operation_name": "\u00a0"}, id="nbsp"),
    pytest.param({"operation_name": "\u2003\u202f"}, id="unicode-whitespace"),
    pytest.param({"operation_name": 7}, id="integer"),
    pytest.param({"operation_name": 7.5}, id="float"),
    pytest.param({"operation_name": True}, id="true"),
    pytest.param({"operation_name": False}, id="false"),
    pytest.param({"operation_name": []}, id="empty-list"),
    pytest.param({"operation_name": ["замена масла"]}, id="list"),
    pytest.param({"operation_name": {}}, id="empty-object"),
    pytest.param({"operation_name": {"name": "замена масла"}}, id="object"),
]


@pytest.fixture(autouse=True)
def isolated_pure_calculation(monkeypatch, tmp_path):
    def denied(*_args, **_kwargs):
        raise AssertionError("pure calculation attempted network or private acquisition")

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


def quote(fields, price=5000, **extra):
    return {**fields, "price_rub": price, "labor_only": True, "includes_parts": False, **extra}


def calculate(rows, observations, **kwargs):
    inputs = deepcopy((rows, observations, kwargs))
    response = labor.calculate_work_price(rows, deepcopy(POLICY), observations=observations, **kwargs)
    assert (rows, observations, kwargs) == inputs
    assert response["execution"] == {"network_calls": 0, "attempts": []}
    return response


def assert_unknown(response, key="unbound", subtotal=0):
    assert response["ok"] is True and response["outcome"] == "partial"
    assert response["data"]["known_subtotal"] == subtotal
    assert response["data"]["total"] is None and response["data"]["range"] is None
    assert response["missing_fields"] == [key + ".price_basis"]
    assert response["data"]["unknown_costs"] == [key + ".price_basis"]
    assert response["data"]["exclusions"] == [{"operation_id": key, "reason": "unknown_labor_or_price"}]


@pytest.mark.parametrize("fields", INVALID_NAMES)
def test_equal_unidentified_names_do_not_supply_a_public_price(fields):
    response = calculate([{"operation_id": "unbound", **fields}], [quote(fields)])
    assert_unknown(response)
    assert response["data"]["operations"] == []


@pytest.mark.parametrize("requested,observed", [(True, 1), (1, True), (False, 0), (0, False), (7, 7.0), (7.0, 7)])
def test_python_numeric_equality_is_not_work_identity(requested, observed):
    response = calculate(
        [{"operation_id": "unbound", "operation_name": requested}], [quote({"operation_name": observed})]
    )
    assert_unknown(response)


@pytest.mark.parametrize(
    "requested,observed",
    [
        ({}, {"operation_name": "unbound"}),
        ({"operation_name": "unbound"}, {}),
        ({"operation_name": None}, {"operation_name": "None"}),
        ({"operation_name": "None"}, {"operation_name": None}),
        ({"operation_name": 7}, {"operation_name": "7"}),
        ({"operation_name": "7"}, {"operation_name": 7}),
        ({"operation_name": ["demo"]}, {"operation_name": "['demo']"}),
        ({"operation_name": "{'name': 'demo'}"}, {"operation_name": {"name": "demo"}}),
    ],
)
def test_public_names_are_not_created_from_ids_or_string_coercion(requested, observed):
    response = calculate([{"operation_id": "unbound", **requested}], [quote(observed, operation_id="unbound")])
    assert_unknown(response)


@pytest.mark.parametrize(
    "fields",
    [
        {},
        {"operation_name": None},
        {"operation_name": "\u00a0"},
        {"operation_name": True},
        {"operation_name": ["demo"]},
        {"operation_name": {"name": "demo"}},
    ],
)
def test_unknown_work_keeps_known_subtotal_without_claiming_a_complete_total(fields):
    rows = [{"operation_id": "known", "operation_name": "known work"}, {"operation_id": "unbound", **fields}]
    observations = [quote({"operation_name": "known work"}, 2000), quote(fields)]
    response = calculate(rows, observations)
    assert_unknown(response, subtotal=2000)
    assert response["data"]["operations"] == [
        {"operation_id": "known", "operation_name": "known work", "price": 2000, "range": [2000, 2000]}
    ]


@pytest.mark.parametrize(
    "name", ["CUSTOM operation #42", "обслуживание ТО №2", "7", "#", "---", "  demo  ", "\u00a0demo\u00a0"]
)
def test_nonempty_literal_names_keep_original_string_identity(name):
    response = calculate([{"operation_id": "literal", "operation_name": name}], [quote({"operation_name": name})])
    assert response["outcome"] == "success"
    assert response["data"]["total"] == 5000 and response["data"]["range"] == [5000, 5000]
    assert response["data"]["operations"][0]["operation_name"] == name


@pytest.mark.parametrize(
    "requested,observed",
    [
        ("demo", "DEMO"),
        ("demo", " demo "),
        (" demo ", "demo"),
        ("7", "07"),
        ("замена масла", " замена масла "),
        ("замена масла", "смена масла"),
    ],
)
def test_name_validation_does_not_add_trim_case_or_alias_matching(requested, observed):
    response = calculate(
        [{"operation_id": "unbound", "operation_name": requested}], [quote({"operation_name": observed})]
    )
    assert_unknown(response)


@pytest.mark.parametrize(
    "requested,observed,bound",
    [
        ("замена тормозных колодок", "замена передних тормозных колодок", True),
        ("замена передних тормозных колодок", "замена тормозных колодок", True),
        ("замена передних тормозных колодок", "замена передних левых тормозных колодок", True),
        ("замена мехатроника DSG", "замена мехатроника DSG 0AM", True),
        ("замена мехатроника DSG 7", "замена мехатроника DSG", True),
        ("замена передних тормозных колодок", "замена задних тормозных колодок", False),
        ("замена мехатроника DSG 6", "замена мехатроника DSG 7", False),
        ("замена мехатроника DSG 0AM", "замена мехатроника DSG 0CW", False),
        ("замена мехатроника DSG 7", "замена мехатроника DSG 0AM", False),
        ("замена масла", "замена масла и фильтра", False),
    ],
)
def test_known_qualification_matching_still_uses_only_compatible_work(requested, observed, bound):
    response = calculate(
        [{"operation_id": "unbound", "operation_name": requested}], [quote({"operation_name": observed})]
    )
    if bound:
        assert response["outcome"] == "success" and response["data"]["total"] == 5000
    else:
        assert_unknown(response)


@pytest.mark.parametrize(
    "fields,total,bounds",
    [
        ({"hours": 0}, 0, [0, 0]),
        ({"hours": 2}, 2000, [2000, 2000]),
        ({}, None, None),
        ({"range_hours": [1, 2]}, 1500, [1000, 2000]),
    ],
)
def test_hourly_rate_does_not_require_public_observation_names(fields, total, bounds):
    rows = [{"operation_id": "id-only", **fields}]
    snapshot = deepcopy(rows)
    response = labor.calculate_work_price(rows, {"basis": "hourly_rate", "version": "test-v1"}, hourly_rate=1000)
    assert response["data"]["total"] == total and response["data"]["range"] == bounds
    assert response["outcome"] == ("partial" if total is None else "success")
    assert rows == snapshot and response["execution"]["network_calls"] == 0


@pytest.mark.parametrize(
    "price,total",
    [(0, 0), (True, None), (False, None), (None, None), (-1, None), (float("inf"), None), ("0", 0), ("2000,5", 2001)],
)
def test_price_validation_remains_independent_from_valid_name_binding(price, total):
    response = calculate(
        [{"operation_id": "unbound", "operation_name": "demo"}], [quote({"operation_name": "demo"}, price)]
    )
    if total is None:
        assert_unknown(response)
    else:
        assert response["outcome"] == "success" and response["data"]["total"] == total


def test_only_bound_labor_only_samples_affect_median_coefficient_and_rounding():
    rows = [{"operation_id": "priced", "operation_name": "demo"}]
    observations = [
        quote({"operation_name": "demo"}, 1000),
        quote({"operation_name": "demo"}, 2000),
        quote({"operation_name": "demo"}, 4000),
        quote({"operation_name": "demo"}, 90000, includes_parts=True),
        quote({"operation_name": "demo"}, 90000, labor_only=False),
        quote({"operation_name": "demo"}, 90000, labor_only=1),
        quote({"operation_name": "demo"}, 90000, includes_parts=0),
        quote({"operation_name": "other"}, 90000),
        quote({}, 90000),
    ]
    policy = {**POLICY, "coefficient": 1.25, "rounding": 100, "currency": "RUB"}
    snapshot = deepcopy((rows, observations, policy))
    response = labor.calculate_work_price(rows, policy, observations=observations)
    assert response["outcome"] == "success"
    assert response["data"]["total"] == 2500 and response["data"]["range"] == [1300, 5000]
    assert response["data"]["policy"] == policy
    assert (rows, observations, policy) == snapshot


def test_even_sample_median_and_distinct_ids_are_preserved():
    rows = [{"operation_id": "one", "operation_name": "demo"}, {"operation_id": "two", "operation_name": "demo"}]
    response = calculate(rows, [quote({"operation_name": "demo"}, 1000), quote({"operation_name": "demo"}, 3000)])
    assert response["data"]["total"] == 4000 and response["data"]["range"] == [2000, 6000]
    assert [row["operation_id"] for row in response["data"]["operations"]] == ["one", "two"]


def test_deduplication_inclusion_and_overlap_keep_their_existing_roles():
    rows = [
        {"operation_id": "package", "operation_name": "package", "included_operations": ["included"]},
        {"operation_id": "included"},
        {"operation_id": "package"},
        {"operation_id": "overlap", "operation_name": "demo", "overlaps_with": ["package"]},
        {"operation_id": "resolved", "operation_name": "demo", "overlaps_with": ["package"], "overlap_resolved": True},
    ]
    response = calculate(rows, [quote({"operation_name": "package"}, 2000), quote({"operation_name": "demo"}, 1000)])
    assert response["data"]["known_subtotal"] == 3000
    assert response["data"]["total"] is None and response["data"]["range"] is None
    assert response["missing_fields"] == ["overlap.overlap_adjustment"]
    assert response["data"]["exclusions"] == [
        {"operation_id": "included", "reason": "duplicate_or_included_operation"},
        {"operation_id": "package", "reason": "duplicate_or_included_operation"},
        {"operation_id": "overlap", "reason": "unresolved_overlap"},
    ]


def test_explicit_unknown_costs_preserve_partial_total_and_inputs():
    response = calculate(
        [{"operation_name": "demo"}], [quote({"operation_name": "demo"}, 2000)], unknown_costs=["extra-work"]
    )
    assert response["outcome"] == "partial" and response["data"]["known_subtotal"] == 2000
    assert response["data"]["total"] is None and response["data"]["range"] is None
    assert response["data"]["unknown_costs"] == response["missing_fields"] == ["extra-work"]


@pytest.fixture
def native_tool():
    server = FastMCP("isolated-public-price-binding")
    register_automotive_tools(server)
    return server._tool_manager.get_tool("calculate_work_price")


@pytest.mark.parametrize(
    "fields,bound",
    [
        ({}, False),
        ({"operation_name": None}, False),
        ({"operation_name": "\u00a0\t"}, False),
        ({"operation_name": 7}, False),
        ({"operation_name": True}, False),
        ({"operation_name": ["demo"]}, False),
        ({"operation_name": {"name": "demo"}}, False),
        ({"operation_name": "CUSTOM operation #42"}, True),
    ],
)
@pytest.mark.parametrize("encoded", [False, True])
def test_registered_native_tool_preserves_nested_json_names_before_binding(native_tool, fields, bound, encoded):
    arguments = {
        "labor": [{"operation_id": "unbound", **fields}],
        "policy": deepcopy(POLICY),
        "observations": [quote(fields)],
    }
    snapshot = deepcopy(arguments)
    # Nested dict values are deliberately not coerced by the SDK argument model.
    model = native_tool.fn_metadata.arg_model.model_validate_json(json.dumps(arguments))
    assert model.model_dump()["labor"] == arguments["labor"]
    assert model.model_dump()["observations"] == arguments["observations"]
    wire = {key: json.dumps(value) for key, value in arguments.items()} if encoded else arguments
    content, response = asyncio.run(native_tool.run(wire, convert_result=True))
    assert json.loads(content[0].text) == response
    if bound:
        assert response["outcome"] == "success" and response["data"]["total"] == 5000
    else:
        assert_unknown(response)
    assert arguments == snapshot and response["execution"]["network_calls"] == 0


@pytest.mark.parametrize("hours", [0, 2])
def test_registered_native_tool_still_prices_id_only_hourly_labor(native_tool, hours):
    arguments = {
        "labor": [{"operation_id": "hourly", "hours": hours}],
        "policy": {"basis": "hourly_rate", "version": "test-v1"},
        "hourly_rate": 1000,
    }
    response = asyncio.run(native_tool.run(arguments))
    assert response["outcome"] == "success" and response["data"]["total"] == hours * 1000
    assert response["data"]["operations"][0]["operation_id"] == "hourly"
