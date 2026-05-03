from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from .config import PROJECT_ROOT

SERVICE_MANAGEMENT_SOURCE_PATH = PROJECT_ROOT / "docs" / "agent" / "service_management_sources.json"

AREA_ALIASES: dict[str, set[str]] = {
    "daily_control": {"daily", "overview", "control", "board", "день", "ежедневно", "контроль", "доска"},
    "parts_procurement": {"parts", "procurement", "supply", "запчасти", "закупки", "снабжение", "детали"},
    "repair_triage": {"repair", "diagnostic", "triage", "ремонт", "диагностика", "разбор", "дефектовка"},
    "staff_management": {"staff", "personnel", "team", "hr", "персонал", "сотрудники", "мастера", "слесари"},
    "customer_flow": {"customer", "client", "approval", "клиент", "клиенты", "согласование", "звонки"},
    "finance_control": {"finance", "money", "cashbox", "payment", "деньги", "финансы", "касса", "оплата"},
    "knowledge_intake": {"knowledge", "files", "intake", "learning", "знания", "файлы", "обучение", "база"},
}


def _normalize_key(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"[^a-z0-9а-яё]+", "_", value.casefold()).strip("_")


@lru_cache(maxsize=1)
def load_service_management_catalog() -> dict[str, Any]:
    if not SERVICE_MANAGEMENT_SOURCE_PATH.exists():
        return {"sources": [], "areas": {}}
    with SERVICE_MANAGEMENT_SOURCE_PATH.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def normalize_area(area: str | None) -> str:
    normalized = _normalize_key(area)
    if not normalized:
        return "daily_control"
    for canonical, aliases in AREA_ALIASES.items():
        if normalized == canonical or normalized in {_normalize_key(alias) for alias in aliases}:
            return canonical
    return normalized


def _source_index() -> dict[str, dict[str, Any]]:
    return {
        str(source.get("source_id") or "").strip(): source
        for source in load_service_management_catalog().get("sources", [])
        if str(source.get("source_id") or "").strip()
    }


def _sources_for_area(area_config: dict[str, Any], city: str, limit: int) -> list[dict[str, Any]]:
    index = _source_index()
    rows: list[dict[str, Any]] = []
    for position, source_id in enumerate(area_config.get("source_ids", [])):
        source = dict(index.get(source_id, {}))
        if not source:
            continue
        source["_score"] = 0
        source["_position"] = position
        city_focus = str(source.get("city_focus") or "").casefold()
        requested_city = city.casefold()
        if city_focus == requested_city or requested_city in city_focus:
            source["_score"] += 10
        if city_focus == "россия" or "россия" in city_focus:
            source["_score"] += 3
        rows.append(source)
    rows.sort(key=lambda source: (-int(source.get("_score", 0)), int(source.get("_position", 0))))
    for source in rows:
        source.pop("_score", None)
        source.pop("_position", None)
    return rows[: max(1, min(limit, 50))]


def _missing_context(area_config: dict[str, Any], context: dict[str, Any]) -> list[str]:
    missing: list[str] = []
    for key in area_config.get("required_context", []):
        if key == "vin_or_chassis" and (context.get("vin") or context.get("chassis")):
            continue
        if key == "crm_board_state":
            continue
        if not context.get(key):
            missing.append(key)
    return missing


def build_service_management_plan(
    *,
    area: str | None = None,
    city: str = "Красноярск",
    vehicle: str | None = None,
    vin: str | None = None,
    chassis: str | None = None,
    part_number: str | None = None,
    part_name: str | None = None,
    urgency: str | None = None,
    role: str | None = None,
    complaint: str | None = None,
    dtc_or_scan: str | None = None,
    engine: str | None = None,
    transmission: str | None = None,
    mileage: str | None = None,
    current_load: str | None = None,
    output_or_hours: str | None = None,
    quality_signal: str | None = None,
    card_id: str | None = None,
    client_contact: str | None = None,
    next_action: str | None = None,
    approval_status: str | None = None,
    repair_orders: str | None = None,
    cashbox: str | None = None,
    payment_status: str | None = None,
    file_path: str | None = None,
    source_type: str | None = None,
    license_status: str | None = None,
    target_playbook: str | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    canonical_area = normalize_area(area)
    catalog = load_service_management_catalog()
    area_config = dict(catalog.get("areas", {}).get(canonical_area, {}))
    resolved_area = canonical_area
    if not area_config:
        resolved_area = "daily_control"
        area_config = dict(catalog.get("areas", {}).get("daily_control", {}))

    context = {
        "vehicle": vehicle,
        "vin": vin,
        "chassis": chassis,
        "part_number": part_number,
        "part_name": part_name,
        "urgency": urgency,
        "role": role,
        "complaint": complaint,
        "dtc_or_scan": dtc_or_scan,
        "engine": engine,
        "transmission": transmission,
        "mileage": mileage,
        "current_load": current_load,
        "output_or_hours": output_or_hours,
        "quality_signal": quality_signal,
        "card_id": card_id,
        "client_contact": client_contact,
        "next_action": next_action,
        "approval_status": approval_status,
        "repair_orders": repair_orders,
        "cashbox": cashbox,
        "payment_status": payment_status,
        "file_path": file_path,
        "source_type": source_type,
        "license_status": license_status,
        "target_playbook": target_playbook,
    }
    warnings = [
        "Use CRM as the source of truth for live cards, clients, repair orders, payments, and board state.",
        "Store only durable conclusions in memory; keep temporary market scans out of memory.",
    ]
    if resolved_area == "parts_procurement" and not part_number:
        warnings.append("Part number is missing; search can start by description, but exact OEM or replacement number is safer.")
    if resolved_area == "staff_management":
        warnings.append("Market salary sources are context only; use internal output, quality, and attendance for decisions.")

    return {
        "ok": True,
        "area": resolved_area,
        "area_input": area,
        "city": city,
        "context": context,
        "required_context": area_config.get("required_context", []),
        "missing_context": _missing_context(area_config, context),
        "sources": _sources_for_area(area_config, city, limit),
        "actions": area_config.get("actions", []),
        "crm_tools": area_config.get("crm_tools", []),
        "kpis": area_config.get("kpis", []),
        "memory_rules": area_config.get("memory_rules", []),
        "warnings": warnings,
        "playbook": "docs/agent/krasnoyarsk_service_management_playbook.md",
        "source_catalog": "docs/agent/service_management_sources.json",
    }
