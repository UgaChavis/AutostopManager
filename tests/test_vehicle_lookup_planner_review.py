from __future__ import annotations

from copy import deepcopy
import socket
import subprocess

import pytest

from autostop_manager import catalog_adapters as catalogs


@pytest.fixture(autouse=True)
def isolated_sources(monkeypatch, tmp_path):
    def denied(*args, **kwargs):
        raise AssertionError("planner controls cannot acquire providers or working state")

    monkeypatch.setenv("AUTOSTOP_MANAGER_ENV_FILE", "/dev/null")
    for key, child in (
        ("AUTOSTOP_MANAGER_DB", "synthetic.sqlite3"),
        ("AUTOSTOP_AUTOMOTIVE_OFFLINE_RUNTIME", "unavailable-runtime"),
        ("AUTOSTOP_J1_CACHE_DIR", "j1"),
        ("AUTOSTOP_J1_VIN_CACHE_DIR", "vin"),
    ):
        monkeypatch.setenv(key, str(tmp_path / child))
    monkeypatch.setenv("AUTOSTOP_J1_VIN_RESEARCH_ENABLED", "0")
    for name in ("connect", "connect_ex", "bind"):
        monkeypatch.setattr(socket.socket, name, denied)
    for name in ("getaddrinfo", "create_connection"):
        monkeypatch.setattr(socket, name, denied)
    monkeypatch.setattr(subprocess, "Popen", denied)
    monkeypatch.setattr(catalogs, "_providers_for_stage", lambda *args, **kwargs: [])
    monkeypatch.setattr(catalogs, "catalog_provider_status", lambda **kwargs: {"providers": []})


def _identity():
    return {
        "vehicle_profile": {
            "make": "DEMO",
            "model": "MODEL_SUPPORTED",
            "model_family": "FAMILY_SUPPORTED",
            "model_year": 2004,
            "engine": "ENGINE_SUPPORTED",
        },
        "family_candidates": [{"make": "DEMO", "model": "SAFE_FAMILY"}],
        "confidence_label": "high",
        "parts_lookup_readiness": {
            "ready_for_family_lookup": True,
            "ready_for_vehicle_lookup": True,
            "ready_for_crm_writeback": False,
        },
    }


def _plan(identity):
    return catalogs.build_oem_parts_provider_plan(
        identifier="WBA" + "0" * 14,
        requested_part="свечи",
        vehicle_identity=identity,
        city="Город",
    )


@pytest.mark.parametrize("structured", [False, True], ids=["flat", "object"])
@pytest.mark.parametrize("field", ["engine", "model_year", "model"])
def test_disputed_fields_cannot_enter_family_queries_in_either_status_shape(field, structured):
    identity = _identity()
    status = {"status": "disputed"} if structured else "disputed"
    identity["field_statuses"] = {field: status}
    original = deepcopy(identity)
    token = str(identity["vehicle_profile"][field])

    result = _plan(identity)

    queries = result["manual_public_search_queries"]
    assert queries
    assert all(token not in row["query"] for row in queries)
    if field == "model":
        assert all("FAMILY_SUPPORTED" not in row["query"] for row in queries)
        assert all("SAFE_FAMILY" in row["query"] for row in queries)
    assert result["lookup_scope"] == "family"
    assert result["live_capability"]["identity_ready_for_vehicle_lookup"] is False
    assert result["live_capability"]["identity_ready_for_crm_writeback"] is False
    assert identity == original
    assert result["field_statuses"] == original["field_statuses"]


@pytest.mark.parametrize("structured", [False, True], ids=["flat", "object"])
def test_supported_fields_remain_in_vehicle_queries_in_either_status_shape(structured):
    identity = _identity()
    status = {"status": "supported"} if structured else "supported"
    identity["field_statuses"] = dict.fromkeys(("model", "model_year", "engine"), status)
    original = deepcopy(identity)

    result = _plan(identity)

    assert result["lookup_scope"] == "vehicle"
    queries = result["manual_public_search_queries"]
    assert len(queries) == 6
    assert all(
        all(token in row["query"] for token in ("MODEL_SUPPORTED", "2004", "ENGINE_SUPPORTED")) for row in queries
    )
    assert result["live_capability"]["identity_ready_for_crm_writeback"] is False
    assert identity == original
