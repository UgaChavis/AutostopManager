"""Ready point production years constrain modification selection independently of MY."""

import asyncio
from copy import deepcopy
import json
import socket

from mcp.server.fastmcp import FastMCP
import pytest

from autostop_manager import automotive_identity as identity
from autostop_manager.automotive_contracts import binding
from autostop_manager.automotive_mcp import register_automotive_tools


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    def denied(*_args, **_kwargs):
        raise AssertionError("ready modification comparison must remain pure")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)


@pytest.fixture(scope="module")
def native_tool():
    server = FastMCP("isolated-modification-production-year")
    register_automotive_tools(server)
    return server._tool_manager.get_tool("compare_vehicle_modifications")


def _compare(context, candidates):
    original = deepcopy((context, candidates))
    response = identity.compare_vehicle_modifications(context, candidates)
    assert (context, candidates) == original
    assert response["ok"] is True
    assert response["execution"] == {"network_calls": 0, "attempts": []}
    assert [row["candidate"] for row in response["data"]["comparisons"]] == candidates
    return response


@pytest.mark.parametrize(
    "known,point,agrees",
    [(2020, 2021, False), (2020, 2020, True), ("2020", "2021", False), ("2020", 2020, True), (2020, "2020", True)],
)
def test_point_year_is_checked_with_existing_integer_and_legacy_string_normalization(known, point, agrees):
    candidate = {"candidate_id": "point", "production_year": point}
    response = _compare({"production_year": known}, [candidate])
    assert response["data"]["selected"] == (candidate if agrees else None)
    assert response["data"]["alternatives"] == ([candidate] if agrees else [])
    assert response["conflicts"] == (
        [] if agrees else [{"field": "production_year", "identity": known, "catalogue": point}]
    )


@pytest.mark.parametrize("reverse", [False, True])
def test_matching_point_survives_among_ordered_candidates(reverse):
    right = {"candidate_id": "right", "production_year": 2020, "raw": {"keep": [None, 0]}}
    wrong = {"candidate_id": "wrong", "production_year": 2021}
    candidates = [right, wrong] if reverse else [wrong, right]
    response = _compare({"production_year": 2020}, candidates)
    assert response["data"]["selected"] == right
    assert response["data"]["alternatives"] == [right]
    assert response["missing_fields"] == []
    assert response["data"]["comparisons"][candidates.index(wrong)]["conflicts"][0]["field"] == "production_year"


@pytest.mark.parametrize(
    "fields,catalogue",
    [
        ({"production_year_from": 2021, "production_year_to": 2021}, [2021.0, 2021.0]),
        ({"production_year_from": 2019, "production_year_to": 2021}, None),
        ({"production_year": 2021, "production_year_from": 2019, "production_year_to": 2021}, 2021),
        ({"production_year": 2020, "production_year_from": 2021, "production_year_to": 2022}, [2021.0, 2022.0]),
        ({"production_year": 2023, "production_year_from": 2021, "production_year_to": 2022}, [2021.0, 2022.0]),
    ],
)
def test_existing_ranges_remain_constraints_without_duplicate_year_conflicts(fields, catalogue):
    candidate = {"candidate_id": "range", **fields}
    response = _compare({"production_year": 2020}, [candidate])
    assert response["data"]["selected"] == (candidate if catalogue is None else None)
    assert response["conflicts"] == (
        [] if catalogue is None else [{"field": "production_year", "identity": 2020, "catalogue": catalogue}]
    )


@pytest.mark.parametrize(
    "context,fields",
    [
        ({"production_year": 2020}, {}),
        ({"production_year": 2020}, {"production_year": None}),
        ({}, {"production_year": 2021}),
        ({"production_year": None}, {"production_year": 2021}),
    ],
)
def test_absent_or_null_year_keeps_existing_unknown_behavior(context, fields):
    candidate = {"candidate_id": "unknown", **fields}
    response = _compare(context, [candidate])
    assert response["data"]["selected"] == candidate
    assert response["conflicts"] == []


@pytest.mark.parametrize("known,point", [(2020, False), (False, 2021), (2020, {"year": 2021})])
def test_unusable_point_year_remains_unknown_without_new_validation_errors(known, point):
    candidate = {"production_year": point}
    response = _compare({"production_year": known}, [candidate])
    assert response["data"]["selected"] == candidate
    assert response["conflicts"] == []


@pytest.mark.parametrize(
    "context,fields",
    [({"model_year": 2020}, {"production_year": 2021}), ({"production_year": 2020}, {"model_year": 2021})],
)
def test_model_year_and_production_year_do_not_substitute_each_other(context, fields):
    candidate = {"candidate_id": "separate-years", **fields}
    response = _compare(context, [candidate])
    assert response["data"]["selected"] == candidate
    assert response["conflicts"] == []


def test_observed_ready_profile_from_reconciliation_keeps_its_point_year_constraint():
    identifier = "DEMO-000001"  # Synthetic frame token; no VIN or decoder read.
    ready = {
        "input_binding": binding(identifier, "frame_number"),
        "vehicle_profile": {"make": "DEMO", "production_year": 2021},
        "field_statuses": {"production_year": "observed"},
    }
    original = deepcopy(ready)
    reconciled = identity.reconcile_vehicle_identity(identifier, [ready], identifier_type="frame_number")
    assert ready == original
    assert reconciled["execution"] == {"network_calls": 0, "attempts": []}
    assert reconciled["data"]["field_statuses"]["production_year"] == "observed"
    profile = reconciled["data"]["vehicle_profile"]
    assert profile["production_year"] == 2021
    compared = _compare({"production_year": 2020}, [profile])
    assert compared["data"]["selected"] is None
    assert compared["data"]["alternatives"] == []
    assert compared["conflicts"][0]["field"] == "production_year"


@pytest.mark.parametrize("encoded", [False, True], ids=["object", "json"])
@pytest.mark.parametrize(
    "context,candidates",
    [
        ({"production_year": 2020}, [{"production_year": 2021}]),
        ({"production_year": "2020"}, [{"production_year": 2020}]),
        (
            {"production_year": 2020},
            [{"candidate_id": "wrong", "production_year": 2021}, {"candidate_id": "right", "production_year": 2020}],
        ),
    ],
)
def test_native_object_and_json_inputs_match_the_pure_comparison(native_tool, encoded, context, candidates):
    arguments = {"context": context, "candidates": candidates}
    original = deepcopy(arguments)
    wire = {key: json.dumps(value) if encoded else value for key, value in arguments.items()}
    content, response = asyncio.run(native_tool.run(wire, convert_result=True))
    assert arguments == original
    assert json.loads(content[0].text) == response
    assert response == _compare(context, candidates)
    if len(candidates) == 2:
        assert response["data"]["selected"] == candidates[1]
    elif context["production_year"] == 2020:
        assert response["data"]["selected"] is None
    else:
        assert response["data"]["selected"] == candidates[0]
