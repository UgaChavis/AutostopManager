from __future__ import annotations

from autostop_manager.service_management import build_service_management_plan, normalize_area


def test_normalize_area_accepts_russian_parts_alias():
    assert normalize_area("запчасти") == "parts_procurement"


def test_parts_procurement_plan_prioritizes_krasnoyarsk_sources():
    result = build_service_management_plan(
        area="parts",
        city="Красноярск",
        part_number="90311-89014",
        vehicle="Lexus RX200T",
        urgency="today",
    )

    assert result["ok"] is True
    assert result["area"] == "parts_procurement"
    assert result["city"] == "Красноярск"
    assert result["sources"]
    assert any(source["source_id"] == "drom_parts" for source in result["sources"])
    assert any(source["city_focus"] == "Красноярск" for source in result["sources"])
    assert "part_number" not in result["missing_context"]


def test_staff_management_plan_has_role_context_and_kpis():
    result = build_service_management_plan(area="персонал", role="автослесарь", city="Красноярск")

    assert result["ok"] is True
    assert result["area"] == "staff_management"
    assert result["kpis"]
    assert any("выработка" in item.casefold() or "hours" in item.casefold() for item in result["kpis"])
    assert any(source["source_id"] in {"hh_ru", "superjob"} for source in result["sources"])
