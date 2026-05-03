from __future__ import annotations

from autostop_manager.mcp_tools import register_manager_memory_tools
from autostop_manager.storage import ManagerMemoryStore


class _FakeServer:
    def __init__(self):
        self.tools = {}

    def tool(self, name: str, description: str = ""):
        def decorator(func):
            self.tools[name] = func
            return func

        return decorator


def test_lookup_original_parts_tool_is_registered(tmp_path):
    server = _FakeServer()
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")

    register_manager_memory_tools(server, store)

    assert "lookup_original_parts" in server.tools
    result = server.tools["lookup_original_parts"]("GXE10-0088644")
    assert result["identifier"]["kind"] == "frame_number"
    assert result["steps"]


def test_recommend_automotive_sources_tool_is_registered(tmp_path):
    server = _FakeServer()
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")

    register_manager_memory_tools(server, store)

    assert "recommend_automotive_sources" in server.tools
    result = server.tools["recommend_automotive_sources"](brand="Toyota", data_type="repair_manuals")
    assert result["ok"] is True
    assert result["sources"]
    assert any(source["source_id"] == "toyota_tis_na" for source in result["sources"])


def test_recommend_fluid_maintenance_sources_tool_is_registered(tmp_path):
    server = _FakeServer()
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")

    register_manager_memory_tools(server, store)

    assert "recommend_fluid_maintenance_sources" in server.tools
    result = server.tools["recommend_fluid_maintenance_sources"](
        brand="Toyota",
        unit="engine_oil",
        year=2019,
        model="Camry",
        engine_code="A25A-FKS",
        market="Russia",
    )
    assert result["ok"] is True
    assert result["unit"] == "engine_oil"
    assert result["lubricant_product_selectors"]


def test_recommend_service_management_actions_tool_is_registered(tmp_path):
    server = _FakeServer()
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")

    register_manager_memory_tools(server, store)

    assert "recommend_service_management_actions" in server.tools
    result = server.tools["recommend_service_management_actions"](
        area="parts",
        city="Красноярск",
        vehicle="Lexus RX200T",
        part_number="90311-89014",
        urgency="today",
    )
    assert result["ok"] is True
    assert result["area"] == "parts_procurement"
    assert any(source["source_id"] == "drom_parts" for source in result["sources"])
