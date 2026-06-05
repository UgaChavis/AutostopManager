from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

from .catalog_adapters import build_oem_parts_provider_plan
from .config import PROJECT_ROOT
from .parts_intent import normalize_part_intent
from .vehicle_identity import decode_vehicle_identity
from .vin_lookup import build_lookup_plan

VIN_OEM_SOURCES_PATH = PROJECT_ROOT / "docs" / "agent" / "vin_oem_sources.json"
PROCUREMENT_SOURCES_PATH = PROJECT_ROOT / "docs" / "agent" / "procurement_price_sources.json"
PLAYBOOK_PATH = "docs/agent/crm_vin_oem_parts_lookup_playbook.md"


@lru_cache(maxsize=1)
def _load_vin_oem_sources() -> dict[str, Any]:
    if not VIN_OEM_SOURCES_PATH.exists():
        return {"sources": [], "integration_backlog": []}
    return json.loads(VIN_OEM_SOURCES_PATH.read_text(encoding="utf-8-sig"))


@lru_cache(maxsize=1)
def _load_procurement_sources() -> dict[str, Any]:
    if not PROCUREMENT_SOURCES_PATH.exists():
        return {"sources": [], "integration_backlog": []}
    return json.loads(PROCUREMENT_SOURCES_PATH.read_text(encoding="utf-8-sig"))


def _compact(value: str | None) -> str:
    return str(value or "").strip()


def _first_present(*values: str | None) -> tuple[str | None, str | None]:
    labels = ["vin", "frame", "body_number"]
    for label, value in zip(labels, values):
        compact = _compact(value)
        if compact:
            return label, compact
    return None, None


def _missing_context(context: dict[str, Any]) -> list[str]:
    missing: list[str] = []
    if not context.get("card_id"):
        missing.append("card_id")
    if not context.get("requested_part"):
        missing.append("requested_part")
    if not (context.get("vin") or context.get("frame") or context.get("body_number")):
        missing.append("vin_or_frame_or_body_number")
    if not (context.get("make") or context.get("vehicle")):
        missing.append("make_or_vehicle")
    return missing


def _catalog_backlog_candidates(limit: int = 8) -> list[dict[str, Any]]:
    registry = _load_vin_oem_sources()
    rows = []
    for source in registry.get("sources", []):
        if source.get("mvp_priority") or source.get("integration_priority"):
            rows.append(source)
    priority = {"high": 0, "medium": 1, "low": 2}
    rows.sort(key=lambda row: priority.get(str(row.get("mvp_priority") or row.get("integration_priority") or "low"), 2))
    return rows[:limit]


def _procurement_backlog_candidates(limit: int = 10) -> list[dict[str, Any]]:
    registry = _load_procurement_sources()
    rows = list(registry.get("crm_vin_oem_parts_pricing_backlog") or registry.get("sources", []))
    priority = {"high": 0, "medium": 1, "low": 2}
    rows.sort(key=lambda row: priority.get(str(row.get("mvp_priority") or row.get("integration_priority") or "low"), 2))
    return rows[:limit]


def _source_digest(source: dict[str, Any]) -> dict[str, Any]:
    return {
        "source_id": source.get("source_id") or source.get("name"),
        "name": source.get("name"),
        "role": source.get("role") or source.get("category") or source.get("kind"),
        "access": source.get("access") or source.get("access_mode"),
        "data": source.get("data") or source.get("use_for") or source.get("inputs"),
        "limits": source.get("limits") or source.get("limitations") or source.get("verification"),
        "env": source.get("env") or source.get("env_names") or source.get("secret_names"),
        "mvp_priority": source.get("mvp_priority") or source.get("integration_priority"),
        "adapter": source.get("adapter"),
        "acceptance": source.get("acceptance") or source.get("test_vin_checks"),
    }


def _crm_note_template() -> str:
    return "\n".join(
        [
            "VIN/OEM подбор:",
            "Авто: <make model, year/build, engine/transmission/market>",
            "VIN/frame source: <CRM field/card text/file>; <identifier type>",
            "Деталь: <part, side/axis/position/quantity>",
            "OEM reference:",
            "- <OEM>: <source + applicability evidence + replacement status>",
            "Replacements/supersession:",
            "- <old/current/replaced-by>: <source>",
            "Selected parts:",
            "- <brand article name>: закупка <price>, рынок РФ <range/avg>, срок <lead time>, source <supplier>, confidence <high|medium|low>",
            "Нужна проверка:",
            "- <missing supplier login/photo/production date/side/stock reserve>",
        ]
    )


def _quote_matrix_schema() -> list[str]:
    return [
        "role",
        "oem_reference",
        "selected_brand",
        "selected_article",
        "part_name",
        "side_axis_position",
        "quantity_basis",
        "source",
        "city_or_warehouse",
        "availability",
        "lead_time",
        "price_procurement",
        "price_public_retail",
        "price_client_sale",
        "price_basis",
        "confidence",
        "needs_confirmation",
    ]


def _manual_writeback_package(resolution: dict[str, Any] | None) -> dict[str, Any] | None:
    if not resolution:
        return None
    candidates = [candidate for candidate in resolution.get("oem_candidates", []) if isinstance(candidate, dict)]
    selected = candidates[0] if candidates else None
    return {
        "write_to_materials_automatically": False,
        "requires_manual_confirmation": True,
        "selected_candidate": selected,
        "rejected_candidates": candidates[1:],
        "confidence": selected.get("confidence_label") if selected else "blocked",
        "source_evidence": {
            "resolution_status": resolution.get("status"),
            "category_resolution": resolution.get("category_resolution"),
            "readiness": resolution.get("readiness"),
            "enrichment": resolution.get("enrichment"),
        },
        "quantity_basis": selected.get("quantity_basis") if selected else (resolution.get("part_intent") or {}).get("quantity_basis"),
        "crm_note": (
            "VIN/OEM подбор готов к ручной проверке: подтвердить OEM-кандидат, применимость, quantity basis и цену перед записью материалов."
            if selected
            else "VIN/OEM подбор не готов к записи материалов: выполнить manual_actions из VinOemResolution."
        ),
    }


def build_crm_vin_parts_lookup_pipeline(
    *,
    card_id: str | None = None,
    requested_part: str | None = None,
    vin: str | None = None,
    frame: str | None = None,
    body_number: str | None = None,
    vehicle: str | None = None,
    make: str | None = None,
    model: str | None = None,
    model_year: int | None = None,
    market: str | None = None,
    engine: str | None = None,
    transmission: str | None = None,
    drivetrain: str | None = None,
    side: str | None = None,
    axle: str | None = None,
    position: str | None = None,
    urgency: str | None = None,
    city: str = "Красноярск",
    limit: int = 10,
    vin_oem_resolution: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the deterministic CRM VIN/OEM parts lookup pipeline.

    This planner does not call supplier APIs and does not write CRM. It returns
    the exact read/lookup/quote/write/verify structure the agent should execute
    with live CRM and current supplier data.
    """

    identifier_source, identifier = _first_present(vin, frame, body_number)
    context = {
        "card_id": _compact(card_id),
        "requested_part": _compact(requested_part),
        "vin": _compact(vin),
        "frame": _compact(frame),
        "body_number": _compact(body_number),
        "vehicle": _compact(vehicle),
        "make": _compact(make),
        "model": _compact(model),
        "model_year": model_year,
        "market": _compact(market),
        "engine": _compact(engine),
        "transmission": _compact(transmission),
        "drivetrain": _compact(drivetrain),
        "side": _compact(side),
        "axle": _compact(axle),
        "position": _compact(position),
        "urgency": _compact(urgency),
        "city": city,
    }
    part_profile = normalize_part_intent(requested_part, axle=axle, side=side, position=position)
    lookup_plan = None
    vehicle_identity = None
    provider_plan = None
    if identifier:
        lookup_plan = build_lookup_plan(identifier, model_year=model_year, make_hint=make)
        vehicle_identity = decode_vehicle_identity(
            identifier,
            crm_context=context,
            model_year=model_year,
            make_hint=make,
            live_vpic=False,
        )
        provider_plan = build_oem_parts_provider_plan(
            identifier=identifier,
            requested_part=context["requested_part"],
            vehicle_identity=vehicle_identity,
            city=city,
        )

    return {
        "ok": True,
        "playbook": PLAYBOOK_PATH,
        "context": context,
        "requested_part_profile": part_profile,
        "missing_context": _missing_context(context),
        "identifier_source": identifier_source,
        "identifier_lookup": lookup_plan,
        "vehicle_identity": vehicle_identity,
        "provider_plan": provider_plan,
        "vin_oem_resolution": vin_oem_resolution,
        "manual_writeback_package": _manual_writeback_package(vin_oem_resolution),
        "pipeline": [
            {
                "step": "read_crm_card_vehicle_data",
                "crm_tools": ["bootstrap_context", "get_card_context", "list_repair_orders", "get_repair_order"],
                "output": "vehicle, VIN/frame/body source, requested part, existing OEM/article, repair-order material context",
            },
            {
                "step": "identify_vehicle_by_vin_frame",
                "manager_tools": ["decode_vehicle_identity", "lookup_original_parts"],
                "checks": ["ISO VIN vs Japan frame/body number vs Korea/KDM VIN", "market", "build window", "engine", "transmission", "drivetrain"],
            },
            {
                "step": "find_oem_for_requested_part",
                "sources": ["official EPC/dealer", "Parts-Catalogs", "PartsAPI", "17VIN", "AUTOPOISK", "PartSouq", "epc-data"],
                "checks": ["group", "side", "axis", "position", "production date", "grade/options", "quantity"],
                "catalog_search_terms": part_profile.get("catalog_search_terms", [])[:8],
                "critical_vehicle_fields": part_profile.get("critical_vehicle_fields", []),
            },
            {
                "step": "find_replacements_and_crosses",
                "sources": ["OEM supersession chain", "PartsAPI/TecDoc", "CROSSBASE-style cross methods", "ZZap replacements", "supplier substitutions"],
                "checks": ["do not upgrade title-match cross to confirmed fitment without applicability evidence"],
            },
            {
                "step": "quote_procurement_and_market_prices",
                "sources": ["ROSSKO", "AutoEuro", "Armtek", "Autopiter", "Emex", "Exist", "Autodoc", "ZZap", "Drom", "Avito"],
                "source_roles": {
                    "procurement_first": ["ROSSKO", "AutoEuro", "Armtek", "Autopiter", "Emex"],
                    "public_retail_reference": ["Exist", "Autodoc", "ZZap", "Drom", "Avito"],
                },
                "checks": [
                    "procurement vs retail vs client sale",
                    "stock",
                    "lead time",
                    "return terms",
                    "package basis",
                    "Exist writes only source Exist, office 905, price/lead-time/analog summary, confidence, and requires-confirmation",
                ],
            },
            {
                "step": "build_quote_matrix",
                "schema": _quote_matrix_schema(),
            },
            {
                "step": "write_structured_result_to_crm_card",
                "crm_tools": ["update_card", "replace_repair_order_materials", "set_card_board_summary"],
                "rules": [
                    "description gets OEM/replacements/quote matrix/source/confidence",
                    "repair-order materials get selected priced part only",
                    "board_summary stays short and excludes raw VIN/client private data",
                ],
            },
            {
                "step": "reopen_and_verify_crm_write",
                "crm_tools": ["get_card_context", "get_repair_order"],
                "checks": ["description persisted", "material total equals manual sum", "selected part line has one price basis", "confidence and needs-confirmation are visible"],
            },
        ],
        "crm_note_template": _crm_note_template(),
        "material_line_rule": {
            "write_to_materials": "selected part with selected price only",
            "keep_in_description": ["OEM reference", "supersession", "crosses/analogs", "rejected candidates", "source matrix"],
            "quantity_rule": "quantity=1 for kit/package/service set; numeric quantity only when price is per piece",
        },
        "confidence_rules": {
            "high": "VIN/frame-specific catalog or supplier confirmation plus independent applicability/price check",
            "medium": "likely OEM or selected part, but one independent check or current stock confirmation is missing",
            "low": "generic, marketplace-only, title-match-only, or missing VIN/frame applicability",
        },
        "safety_rules": [
            "Do not invent OEM, cross, supersession, price, stock, or fitment.",
            "Do not store raw VIN/client data, supplier secrets, or raw CRM records in durable memory or Git.",
            "Do not place supplier orders or change financial CRM records without a separate explicit owner command.",
        ],
        "catalog_backlog_candidates": [_source_digest(source) for source in _catalog_backlog_candidates(limit)],
        "procurement_backlog_candidates": [_source_digest(source) for source in _procurement_backlog_candidates(limit)],
    }
