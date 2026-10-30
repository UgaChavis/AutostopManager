"""Normalization retains exact inclusion links without merging same-name work."""

import asyncio
from copy import deepcopy
import json
import socket
import sqlite3
import subprocess

from mcp.server.fastmcp import FastMCP
import pytest

from autostop_manager import automotive_labor as labor
from autostop_manager.automotive_mcp import register_automotive_tools


POLICY = {"basis": "hourly_rate", "version": "synthetic-inclusion-v1"}


def row(name, hours=1, **fields):
    return {"operation_name": name, "hours": hours, "source": {"provider": "synthetic"}, **fields}


@pytest.fixture(scope="module")
def native_tools():
    server = FastMCP("isolated-labor-inclusion")
    register_automotive_tools(server)
    return {name: server._tool_manager.get_tool(name) for name in ("normalize_labor_time", "calculate_work_price")}


@pytest.fixture
def isolated_loop(monkeypatch):
    # Windows loop construction uses a local socket pair; block I/O afterwards.
    loop = asyncio.new_event_loop()

    def denied(*args, **kwargs):
        raise AssertionError("pure labor handoff must not access network, storage or subprocesses")

    for name in ("connect", "connect_ex"):
        monkeypatch.setattr(socket.socket, name, denied)
    for name in ("create_connection", "getaddrinfo"):
        monkeypatch.setattr(socket, name, denied)
    monkeypatch.setattr(sqlite3, "connect", denied)
    monkeypatch.setattr(subprocess, "Popen", denied)
    yield loop
    loop.close()


@pytest.fixture(params=["direct", "native", "native-json"])
def call(request, native_tools, isolated_loop):
    def invoke(name, **arguments):
        before = deepcopy(arguments)
        if request.param == "direct":
            response = getattr(labor, name)(**arguments)
        else:
            wire = (
                {
                    key: json.dumps(value) if isinstance(value, (list, dict)) else value
                    for key, value in arguments.items()
                }
                if request.param == "native-json"
                else arguments
            )
            content, response = isolated_loop.run_until_complete(native_tools[name].run(wire, convert_result=True))
            assert json.loads(content[0].text) == response
        assert arguments == before
        assert response["execution"] == {"network_calls": 0, "attempts": []}
        return response

    return invoke


def calculate(call, rows):
    return call("calculate_work_price", labor=rows, policy=POLICY, hourly_rate=1000)


@pytest.mark.parametrize("identity", [{}, {"operation_id": None}, {"operation_id": ""}])
@pytest.mark.parametrize("reverse", [False, True])
def test_unique_name_reference_survives_handoff_and_row_order(call, identity, reverse):
    rows = [row("package", 2, included_operations=["included", "included"]), row("included", **identity)]
    if reverse:
        rows.reverse()
    original = calculate(call, rows)
    normalized = call("normalize_labor_time", rows=rows)
    assert normalized["outcome"] == "success"
    ready = normalized["data"]["labor"]
    package = next(item for item in ready if item["operation_name"] == "package")
    included = next(item for item in ready if item["operation_name"] == "included")
    assert package["included_operations"] == [included["operation_id"], included["operation_id"]]
    assert normalized["data"]["overlaps"] == [
        {
            "operation_id": package["operation_id"],
            "included_operations": package["included_operations"],
            "overlaps_with": [],
        }
    ]
    actual = calculate(call, ready)
    assert original["data"]["total"] == actual["data"]["total"] == 2000
    assert actual["data"]["exclusions"] == [
        {"operation_id": included["operation_id"], "reason": "duplicate_or_included_operation"}
    ]


@pytest.mark.parametrize("name_field", ["workName", "operation_name", "operation", "name"])
def test_selected_name_alias_is_used_without_case_or_whitespace_guessing(call, name_field):
    rows = [
        row("package", 2, included_operations=["  Exact  "]),
        {name_field: "  Exact  ", "hours": 1, "scope": "right"},
        row("Exact", 3, scope="left"),
        row("  exact  ", 4),
    ]
    normalized = call("normalize_labor_time", rows=rows, source={"provider": "synthetic"})
    ready = normalized["data"]["labor"]
    assert ready[0]["included_operations"] == [ready[1]["operation_id"]]
    assert len({item["operation_id"] for item in ready}) == 4
    assert calculate(call, ready)["data"]["total"] == 9000


@pytest.mark.parametrize("reverse", [False, True])
def test_repeated_names_are_distinct_without_a_reference(call, reverse):
    rows = [row("same", 1, scope="left"), row("same", 2, scope="right")]
    if reverse:
        rows.reverse()
    normalized = call("normalize_labor_time", rows=rows)
    assert normalized["outcome"] == "success"
    ready = normalized["data"]["labor"]
    assert len({item["operation_id"] for item in ready}) == 2
    assert [item["scope"] for item in ready] == [item["scope"] for item in rows]
    actual = calculate(call, ready)
    assert actual["data"]["total"] == 3000 and actual["data"]["exclusions"] == []


@pytest.mark.parametrize("reverse", [False, True])
def test_ambiguous_name_reference_requires_explicit_ids(call, reverse):
    rows = [row("package", 2, included_operations=["same"]), row("same", scope="left"), row("same", 3, scope="right")]
    if reverse:
        rows.reverse()
    response = call("normalize_labor_time", rows=rows)
    assert response["outcome"] == "invalid_input" and response["ok"] is False
    assert response["missing_fields"] == ["rows.included_operations"]
    assert response["data"] == {}


@pytest.mark.parametrize("explicit_id", [0, "same"])
def test_exact_explicit_id_takes_precedence_over_implicit_name(call, explicit_id):
    rows = [
        row("package", 2, operation_id="package", included_operations=[explicit_id]),
        row("different label", operation_id=explicit_id),
        row(str(explicit_id), 4),
        row(str(explicit_id), 5),
    ]
    normalized = call("normalize_labor_time", rows=rows)
    assert normalized["outcome"] == "success"
    ready = normalized["data"]["labor"]
    assert ready[0]["operation_id"] == "package" and ready[1]["operation_id"] == str(explicit_id)
    assert ready[0]["included_operations"] == [explicit_id]
    actual = calculate(call, ready)
    assert actual["data"]["total"] == 11000
    assert actual["data"]["exclusions"] == [
        {"operation_id": str(explicit_id), "reason": "duplicate_or_included_operation"}
    ]


def test_name_is_not_an_alias_for_an_explicit_different_id(call):
    rows = [row("package", 2, included_operations=["same"]), row("same", operation_id="explicit")]
    normalized = call("normalize_labor_time", rows=rows)
    ready = normalized["data"]["labor"]
    assert ready[0]["included_operations"] == ["same"]
    assert ready[1]["operation_id"] == "explicit"
    assert calculate(call, ready)["data"]["total"] == 3000


def test_mixed_reference_order_and_generated_ids_are_preserved(call):
    rows = [row("package", 2), row("included"), row("zero", operation_id=0), row("same label", 3)]
    baseline = call("normalize_labor_time", rows=rows)["data"]["labor"]
    generated_id = baseline[3]["operation_id"]
    rows[0]["included_operations"] = ["outside", 0, "included", generated_id, "included"]
    normalized = call("normalize_labor_time", rows=rows)
    ready = normalized["data"]["labor"]
    assert [item["operation_id"] for item in ready] == [item["operation_id"] for item in baseline]
    assert ready[0]["included_operations"] == [
        "outside",
        0,
        ready[1]["operation_id"],
        generated_id,
        ready[1]["operation_id"],
    ]
    assert calculate(call, ready)["data"]["total"] == 2000


def test_ignored_lower_priority_name_is_not_a_reference_alias(call):
    rows = [row("package", 2, included_operations=["ignored"]), row("ignored", workName="selected")]
    normalized = call("normalize_labor_time", rows=rows)
    ready = normalized["data"]["labor"]
    assert ready[1]["operation_name"] == "selected"
    assert ready[0]["included_operations"] == ["ignored"]
    assert calculate(call, ready)["data"]["total"] == 3000


def test_dangling_references_and_overlap_metadata_remain_literal(call):
    rows = [
        row("package", 2, included_operations=["outside", 17], overlaps_with=["unlisted"], overlap_resolved=True),
    ]
    normalized = call("normalize_labor_time", rows=rows)
    assert normalized["outcome"] == "success"
    ready = normalized["data"]["labor"]
    assert len(ready) == 1
    assert ready[0]["included_operations"] == ["outside", 17]
    assert ready[0]["overlaps_with"] == ["unlisted"] and ready[0]["overlap_resolved"] is True
    assert calculate(call, ready)["data"]["total"] == 2000


@pytest.mark.parametrize("cyclic", [False, True])
def test_implicit_inclusion_graph_still_rejects_self_and_cycles(call, cyclic):
    rows = (
        [row("A", included_operations=["B"]), row("B", included_operations=["A"])]
        if cyclic
        else [row("A", included_operations=["A"])]
    )
    normalized = call("normalize_labor_time", rows=rows)
    assert normalized["outcome"] == "success"
    response = calculate(call, normalized["data"]["labor"])
    assert response["outcome"] == "invalid_input"
    assert response["missing_fields"] == ["labor.included_operations"]
