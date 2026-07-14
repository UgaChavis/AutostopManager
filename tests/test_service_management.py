from __future__ import annotations

import json

from autostop_manager.service_management import (
    build_service_management_plan,
    load_service_management_catalog,
    normalize_area,
)


def test_normalize_area_accepts_russian_parts_alias():
    assert normalize_area("запчасти") == "parts_procurement"


def test_parts_procurement_plan_prioritizes_krasnoyarsk_sources():
    result = build_service_management_plan(
        area="parts",
        city="Красноярск",
        part_number="90311-89014",
        vehicle="Lexus RX200T",
        urgency="today",
        limit=20,
    )

    assert result["ok"] is True
    assert result["area"] == "parts_procurement"
    assert result["city"] == "Красноярск"
    assert result["sources"]
    assert any(source["source_id"] == "drom_parts" for source in result["sources"])
    assert any(source["source_id"] == "rossko" for source in result["sources"])
    assert any(source["source_id"] == "autoeuro_api" for source in result["sources"])
    assert any(source["city_focus"] == "Красноярск" for source in result["sources"])
    assert "part_number" not in result["missing_context"]


def test_staff_management_plan_has_role_context_and_kpis():
    result = build_service_management_plan(area="персонал", role="автослесарь", city="Красноярск")

    assert result["ok"] is True
    assert result["area"] == "staff_management"
    assert result["kpis"]
    assert any("выработка" in item.casefold() or "hours" in item.casefold() for item in result["kpis"])
    assert any(source["source_id"] in {"hh_ru", "superjob"} for source in result["sources"])


def test_service_catalog_merges_procurement_without_duplicate_ids():
    catalog = load_service_management_catalog()
    source_ids = [source["source_id"] for source in catalog["sources"]]

    assert "rossko" in source_ids
    assert "hh_ru" in source_ids
    assert "mikado" not in source_ids
    assert len(source_ids) == len(set(source_ids))
    for area in catalog["areas"].values():
        assert set(area["source_ids"]) <= set(source_ids)


def test_service_management_plan_redacts_sensitive_context_from_public_output():
    raw_vin = "JTEBU3FJX05027767"
    raw_contact = "+7 913 000-00-00 client@example.test"
    raw_repair_orders = "ZN-42 materials 10000"
    raw_cashbox = "cashbox delta 5000"
    raw_file_path = "/private/clients/client@example.test/invoice.pdf"
    result = build_service_management_plan(
        area="finance_control",
        vin=raw_vin,
        client_contact=raw_contact,
        repair_orders=raw_repair_orders,
        cashbox=raw_cashbox,
        payment_status="unpaid 15000",
        file_path=raw_file_path,
    )

    rendered = json.dumps(result, ensure_ascii=False)
    assert raw_vin not in rendered
    assert raw_contact not in rendered
    assert raw_repair_orders not in rendered
    assert raw_cashbox not in rendered
    assert raw_file_path not in rendered
    assert result["context"]["vin"] == "JTE***767"
    assert result["context"]["client_contact"] == "<provided:redacted>"
    assert result["context"]["repair_orders"] == "<provided:redacted>"
    assert result["context"]["cashbox"] == "<provided:redacted>"
    assert result["privacy"]["raw_sensitive_context_redacted"] is True
