from __future__ import annotations

import asyncio
from copy import deepcopy
import json
from pathlib import Path
import socket

from autostop_manager import vin_sources
from autostop_manager.knowledge_base import search_knowledge_base, sync_knowledge_base
from autostop_manager.mcp_server import build_server
from autostop_manager.storage import ManagerMemoryStore
from autostop_manager.vin_lookup import build_lookup_plan


def test_public_routes_do_not_replace_a_missing_registry(monkeypatch):
    monkeypatch.setattr(vin_sources, "load_source_registry", lambda: {"sources": []})
    assert vin_sources.sources_for_make("Toyota") == []
    assert vin_sources.sources_for_inputs("frame_number") == []


def test_public_route_metadata_has_one_registry_source(monkeypatch):
    registry = deepcopy(vin_sources.load_source_registry())
    original = deepcopy(registry)
    monkeypatch.setattr(vin_sources, "load_source_registry", lambda: registry)
    index = vin_sources.source_index()
    for source_id, aliases in vin_sources.PUBLIC_CATALOG_SOURCE_ALIASES.items():
        stored = next(row for row in registry["sources"] if row.get("source_id") == source_id)
        assert index[source_id] == stored
        assert "diagram_url" in stored["outputs"]
        for alias in aliases:
            assert index[alias] == stored
    for make in ("Toyota", "Suzuki", "Mitsubishi"):
        source_ids = [row.get("source_id") for row in vin_sources.sources_for_make(make)]
        assert source_ids.count("partsouq_catalog") == source_ids.count("amayama_catalog") == 1
    assert registry == original


def test_corporate_make_names_keep_japanese_public_fallbacks():
    for make in ("Toyota Motor Corporation", "Mitsubishi Motors Corporation"):
        source_ids = {source.get("source_id") for source in vin_sources.sources_for_make(make)}
        assert {"partsouq_catalog", "amayama_catalog"} <= source_ids


def test_decoder_timeout_keeps_public_routes_and_no_confirmed_fitment(monkeypatch):
    timeouts = []

    def unavailable(_request, timeout):
        timeouts.append(timeout)
        raise TimeoutError("synthetic timeout")

    monkeypatch.setattr("autostop_manager.vin_lookup.urlopen", unavailable)
    result = build_lookup_plan("JT000000000000001", make_hint="Toyota", part_name="фильтр", live_vpic=True)
    assert timeouts and len(timeouts) <= 2
    assert all(0 < timeout <= 10 for timeout in timeouts)
    names = {row["source_name"] for row in result["catalog_routes"]}
    assert {"PartSouq manual catalog", "Amayama public catalog"} <= names
    assert result["oem_candidates"] == []
    assert "JT000000000000001" not in json.dumps(result)


def test_registered_mcp_plan_runs_without_network_or_emex(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOSTOP_MANAGER_ENV_FILE", "/dev/null")
    monkeypatch.setenv("AUTOSTOP_MANAGER_DB", str(tmp_path / "mcp.sqlite3"))

    def forbidden(*_args, **_kwargs):
        raise AssertionError("planning must not open a network connection")

    server = build_server()
    assert "emex_price_lookup" not in server._tool_manager._tools
    with asyncio.Runner() as runner:
        runner.get_loop()
        monkeypatch.setattr(socket.socket, "connect", forbidden)
        result = runner.run(
            server.call_tool(
                "plan_oem_parts_providers",
                {
                    "identifier": "",
                    "requested_part": "воздушный фильтр",
                    "vehicle_identity": {"vehicle_profile": {"make": "Toyota", "model": "Corolla", "model_year": 2008}},
                },
            )
        )
    # FastMCP returns textual and structured content from the real registered callable.
    payload = result[1]
    ids = {row["source_id"] for row in payload["manual_public_search_queries"]}
    assert {"partsouq_catalog", "amayama_catalog"} <= ids


def test_current_parts_skill_is_loaded_into_disposable_knowledge_index(tmp_path):
    store = ManagerMemoryStore(tmp_path / "knowledge.sqlite3")
    result = sync_knowledge_base(store)
    assert result["ok"] is True
    items = search_knowledge_base(store, "PartSouq Amayama", limit=50)["items"]
    skill_path = ".agents/skills/manage-autostop-store/SKILL.md"
    assert any(item["path"] == skill_path for item in items)
    root = Path(__file__).resolve().parents[1]
    skill = root / skill_path
    text = skill.read_text(encoding="utf-8")
    assert "docs/agent/vin_oem_sources.json" in text
