from __future__ import annotations

from typing import Any

from .vin_parts_benchmark import benchmark_vin_parts_lookup


EUROPEAN_BRAND_ROUTES = {
    "audi": [
        {
            "source_id": "partslink24_or_oem_epc",
            "name": "partslink24 / ETKA",
            "role": "VIN-specific VAG EPC route for PR/options, production date, catalog group, and OEM part numbers.",
            "access": "manual_subscription",
            "acceptance": "Vehicle is selected by VIN or exact market catalog; PR/options and production split are visible.",
        }
    ],
    "volkswagen": [
        {
            "source_id": "partslink24_or_oem_epc",
            "name": "partslink24 / ETKA",
            "role": "VIN-specific VAG EPC route for PR/options, production date, catalog group, and OEM part numbers.",
            "access": "manual_subscription",
            "acceptance": "Vehicle is selected by VIN or exact market catalog; PR/options and production split are visible.",
        }
    ],
    "skoda": [
        {
            "source_id": "partslink24_or_oem_epc",
            "name": "partslink24 / ETKA",
            "role": "VIN-specific Skoda/VAG EPC route for PR/options, production date, catalog group, and OEM part numbers.",
            "access": "manual_subscription",
            "acceptance": "Vehicle is selected by VIN or exact market catalog; PR/options and production split are visible.",
        }
    ],
    "mercedes-benz": [
        {
            "source_id": "partslink24_or_oem_epc",
            "name": "Mercedes EPC / partslink24",
            "role": "VIN-specific Mercedes EPC route for datacard/options and exact OEM applicability.",
            "access": "manual_subscription",
            "acceptance": "Datacard/options, production date, engine variant, and catalog group are visible.",
        }
    ],
    "bmw": [
        {
            "source_id": "partslink24_or_oem_epc",
            "name": "BMW ETK / AIR / partslink24",
            "role": "VIN-specific BMW route for option codes, production date, and OEM part applicability.",
            "access": "manual_subscription",
            "acceptance": "VIN-specific production date/options and BMW part group are visible.",
        }
    ],
}

ASIAN_BRAND_ROUTES = {
    "toyota": ("Toyota/Lexus EPC", "Toyota frame/VIN catalog route; confirm production date, model code, grade, and OEM group."),
    "lexus": ("Toyota/Lexus EPC", "Toyota/Lexus VIN/frame catalog route; confirm production date, model code, grade, and OEM group."),
    "honda": ("Honda EPC / epc-data", "Honda VIN/frame catalog route; confirm frame form, production date, trim, and OEM group."),
    "nissan": ("Nissan EPC / epc-data", "Nissan VIN/frame catalog route; confirm model code, production date, trim, and OEM group."),
    "mazda": ("Mazda EPC", "Mazda VIN/chassis catalog route; confirm engine code, production date, drive, and OEM group."),
    "mitsubishi": ("Mitsubishi ASA/EPC", "Mitsubishi VIN/frame catalog route; confirm engine, transmission, body, and OEM group."),
    "suzuki": ("Suzuki EPC", "Suzuki frame catalog route; confirm model code, production date, grade, and OEM group."),
}

COMMON_OEM_ROUTES = [
    {
        "source_id": "parts_catalogs_api",
        "name": "Parts-Catalogs API",
        "role": "Automated VIN/frame -> vehicle -> group -> OEM candidate route.",
        "access": "api_key",
        "env_names": ["PARTS_CATALOGS_API_KEY", "PARTS_CATALOGS_BASE_URL"],
        "acceptance": "Returns VIN/frame-specific vehicle and requested part group with production/option evidence.",
    },
    {
        "source_id": "vin17_api",
        "name": "17VIN API",
        "role": "Automated second-source VIN -> EPC/profile -> common parts / part search route.",
        "access": "account_token",
        "env_names": ["VIN17_ACCOUNT", "VIN17_SECRET"],
        "acceptance": "Returns an EPC/profile and part candidates or replacement chain with source provenance.",
    },
    {
        "source_id": "partsapi_ru",
        "name": "PARTSAPI.RU",
        "role": "VIN/OE decode plus applicability/cross checks after or during OEM lookup.",
        "access": "api_key",
        "env_names": ["PARTSAPI_KEY", "PARTSAPI_BASE_URL"],
        "acceptance": "VINdecodeOE/getPartsbyVIN returns candidates; applicability/crosses stay separate from confirmed fitment.",
    },
]

SUPPLIER_ROUTES = [
    {
        "source_id": "rossko",
        "name": "ROSSKO",
        "stage": "procurement_price",
        "env_names": ["ROSSKO_KEY1", "ROSSKO_KEY2"],
        "query_after": "OEM reference or selected brand/article",
        "acceptance": "Krasnoyarsk stock/procurement price, lead time, return terms, and quote id/warehouse are visible.",
    },
    {
        "source_id": "autoeuro_api",
        "name": "AutoEuro API",
        "stage": "procurement_price",
        "env_names": ["AUTOEURO_API_KEY"],
        "query_after": "selected brand/article",
        "acceptance": "Supplier price, stock, delivery, and cancel/return constraints are visible.",
    },
    {
        "source_id": "armtek",
        "name": "Armtek",
        "stage": "procurement_price",
        "env_names": ["ARMTEK_LOGIN", "ARMTEK_PASSWORD"],
        "query_after": "selected brand/article",
        "acceptance": "B2B price, stock, lead time, and return terms are visible.",
    },
    {
        "source_id": "autopiter",
        "name": "Autopiter",
        "stage": "procurement_price",
        "env_names": ["AUTOPITER_USER_ID", "AUTOPITER_PASSWORD"],
        "query_after": "selected brand/article",
        "acceptance": "Russia-wide price, stock, delivery to Krasnoyarsk, and brand disambiguation are visible.",
    },
    {
        "source_id": "exist",
        "name": "Exist",
        "stage": "procurement_price",
        "env_names": [],
        "query_after": "selected brand/article or OEM reference",
        "adapter": "exist-price-lookup",
        "acceptance": (
            "Public retail reference only: keep office 905, price/lead-time/analog summary, confidence, "
            "and requires-confirmation in internal quote evidence; expose no basket/private-cabinet data "
            "and no uncertainty metadata in a public card description."
        ),
    },
    {
        "source_id": "zzap",
        "name": "ZZap",
        "stage": "market_price",
        "env_names": ["ZZAP_API_KEY"],
        "query_after": "OEM reference or selected brand/article",
        "acceptance": "3-5 current RF offers or market-stat range; separated from procurement price.",
    },
]


def _profile_make(vehicle_profile: dict[str, Any]) -> str:
    return str(vehicle_profile.get("make") or "").strip().casefold()


def _brand_epc_routes(vehicle_profile: dict[str, Any], identifier_kind: str) -> list[dict[str, Any]]:
    make = _profile_make(vehicle_profile)
    routes: list[dict[str, Any]] = []
    routes.extend(EUROPEAN_BRAND_ROUTES.get(make, []))
    if make in ASIAN_BRAND_ROUTES:
        name, role = ASIAN_BRAND_ROUTES[make]
        routes.append(
            {
                "source_id": f"{make}_brand_epc_manual",
                "name": name,
                "role": role,
                "access": "manual_or_subscription",
                "acceptance": "The source shows exact catalog vehicle, production split, requested group, and OEM part candidate.",
            }
        )
    if identifier_kind in {"frame_number", "market_code"} or make in ASIAN_BRAND_ROUTES:
        routes.append(
            {
                "source_id": "epc_data_manual",
                "name": "epc-data / public EPC mirror",
                "role": "Manual fallback for frame/VIN catalog entry and diagram/group navigation.",
                "access": "public_manual",
                "acceptance": "Use as a fallback only; cross-check with another EPC/API before high-confidence CRM writeback.",
            }
        )
    routes.append(
        {
            "source_id": "partsouq_manual",
            "name": "PartSouq manual catalog",
            "role": "Manual fallback for diagram/OEM sanity check and international availability reference.",
            "access": "public_manual",
            "acceptance": "Use as source check only; do not treat international availability as Krasnoyarsk procurement.",
        }
    )
    return routes


def _missing_env_for_stage(blockers: list[dict[str, Any]], stage: str) -> list[str]:
    names = set()
    for blocker in blockers:
        if blocker.get("stage") == stage:
            for field in ("missing_env_names", "missing_env"):
                value = blocker.get(field)
                if isinstance(value, (list, tuple, set)):
                    names.update(str(name).strip() for name in value if str(name).strip())
                elif value not in (None, ""):
                    names.add(str(value).strip())
    return sorted(names)


def _item_status(item: dict[str, Any]) -> str:
    if item["live_capability"].get("can_complete_full_auto_lookup_now"):
        return "ready_for_live_auto_oem_cross_and_price_lookup"
    if not item["requested_part"].get("recognized"):
        return "needs_part_intent_clarification_before_catalog_search"
    if item["requested_part"].get("clarification_required"):
        return "needs_part_position_clarification_before_catalog_search"
    if item["identity"].get("ready_for_oem_candidate_lookup") and item["requested_part"].get("recognized"):
        if not item["identity"].get("ready_for_crm_writeback"):
            return "ready_for_oem_candidate_lookup_needs_manual_confirmation"
        return "ready_for_manual_epc_and_market_search_but_live_credentials_missing"
    if not item["identity"].get("ready_for_oem_candidate_lookup"):
        return "needs_identity_confirmation_before_parts_search"
    return "needs_part_intent_clarification_before_catalog_search"


def _search_terms(item: dict[str, Any]) -> list[str]:
    vehicle = item["identity"].get("vehicle_profile") or {}
    part_terms = item["requested_part"].get("catalog_search_terms") or []
    vehicle_bits = [
        vehicle.get("make"),
        vehicle.get("model") or vehicle.get("model_family"),
        vehicle.get("platform"),
        vehicle.get("model_year"),
        vehicle.get("engine"),
    ]
    vehicle_text = " ".join(str(value).strip() for value in vehicle_bits if value not in (None, ""))
    terms = []
    for part in part_terms[:5]:
        phrase = " ".join(value for value in [vehicle_text, str(part).strip()] if value).strip()
        if phrase:
            terms.append(phrase)
    return list(dict.fromkeys(terms))


def _work_order_item(item: dict[str, Any]) -> dict[str, Any]:
    vehicle_profile = item["identity"].get("vehicle_profile") or {}
    identifier_kind = item["identifier"].get("kind") or "unknown"
    oem_resolution = item.get("oem_resolution") or {}
    status = oem_resolution.get("status") or _item_status(item)
    oem_missing = _missing_env_for_stage(item.get("blockers", []), "oem_catalog")
    price_missing = _missing_env_for_stage(item.get("blockers", []), "procurement_price")
    resolver_gate = oem_resolution.get("crm_writeback_gate") or {}
    return {
        "index": item["index"],
        "identifier": item["identifier"],
        "vehicle_profile": vehicle_profile,
        "requested_part": item["requested_part"],
        "status": status,
        "search_terms": _search_terms(item),
        "oem_lookup_routes": {
            "automated_first": COMMON_OEM_ROUTES,
            "brand_or_market_manual": _brand_epc_routes(vehicle_profile, identifier_kind),
            "missing_live_env_names": oem_missing,
        },
        "prepared_api_checks": item["prepared_calls"],
        "oem_resolution": oem_resolution or None,
        "next_manual_actions": oem_resolution.get("manual_actions", []),
        "cross_and_applicability_checks": [
            {
                "step": "confirm_oem_applicability",
                "tool_or_source": "partsapi_catalog_lookup(operation='oe_applicability') or EPC source",
                "requires": "OEM candidate from VIN/frame-specific catalog",
                "acceptance": "Applicability explicitly matches vehicle, production split, side/axis/position, and kit contents.",
            },
            {
                "step": "find_crosses",
                "tool_or_source": "partsapi_catalog_lookup(operation='crosses_with_brand') / supplier catalog / ZZap replacements",
                "requires": "OEM or selected brand/article",
                "acceptance": "Crosses are marked as confirmed only after applicability, not from title match alone.",
            },
        ],
        "procurement_lookup_routes": {
            "supplier_sequence": SUPPLIER_ROUTES,
            "missing_live_env_names": price_missing,
            "public_market_queries": item["manual_public_search"]["queries"],
        },
        "crm_writeback_gate": {
            "can_write_final_material_line_now": bool(resolver_gate.get("can_write_final_material_line_now"))
            if resolver_gate
            else bool(item["live_capability"].get("can_complete_full_auto_lookup_now")),
            "can_run_read_only_oem_candidate_lookup": bool(
                oem_resolution.get("readiness", {}).get("ready_for_oem_candidate_lookup")
                if oem_resolution
                else item["identity"].get("ready_for_oem_candidate_lookup") and item["requested_part"].get("recognized")
            ),
            "requires_manual_confirmation_before_writeback": bool(resolver_gate.get("requires_manual_confirmation_before_writeback", True))
            if resolver_gate
            else not bool(item["identity"].get("ready_for_crm_writeback")),
            "can_prepare_manual_writeback": bool(resolver_gate.get("can_prepare_manual_writeback", False)),
            "allowed_without_live_credentials": "Write only a preliminary quote matrix/status with needs-confirmation; do not write final selected material price.",
            "final_material_requires": [
                "VIN/frame-specific OEM or selected-part applicability evidence",
                "selected brand/article and quantity basis",
                "supplier-confirmed procurement price or explicit owner-approved retail benchmark",
                "re-opened CRM repair order total verification after write",
            ],
        },
        "acceptance_checklist": [
            "identity source and vehicle profile recorded",
            "part group, side/axis/position, and quantity basis recorded",
            "OEM candidate has source, production/options evidence, and confidence",
            "cross/analog is separated from OEM reference and has applicability evidence",
            "procurement, retail benchmark, and client sale price are not mixed",
            "raw VIN/frame is not stored in durable memory, docs, tests, or board summary",
        ],
    }


def build_vin_parts_work_order(
    items: list[dict[str, Any]],
    *,
    requested_part: str,
    city: str = "Красноярск",
    live_vpic: bool = True,
    use_vpic_batch: bool = True,
    live_partsapi_identity: bool = False,
    live_partsapi_oem: bool = False,
    resolve_oem: bool = False,
    max_live_calls: int = 3,
    max_candidates: int = 3,
    partsapi_category_index: str | None = None,
) -> dict[str, Any]:
    benchmark = benchmark_vin_parts_lookup(
        items,
        requested_part=requested_part,
        city=city,
        live_vpic=live_vpic,
        use_vpic_batch=use_vpic_batch,
        live_partsapi_identity=live_partsapi_identity,
        live_partsapi_oem=live_partsapi_oem,
        resolve_oem=resolve_oem,
        max_live_calls=max_live_calls,
        max_candidates=max_candidates,
        partsapi_category_index=partsapi_category_index,
    )
    work_items = [_work_order_item(item) for item in benchmark["items"]]
    count = int(benchmark["summary"].get("count") or 0)
    full_auto_count = int(benchmark["summary"].get("full_auto_lookup_count") or 0)
    return {
        "ok": True,
        "mode": "read_only_vin_parts_work_order",
        "city": city,
        "benchmark_summary": benchmark["summary"],
        "work_order_summary": {
            "count": len(work_items),
            "ready_for_manual_epc_and_market_search_count": sum(
                1 for item in work_items if item["status"] == "ready_for_manual_epc_and_market_search_but_live_credentials_missing"
            ),
            "ready_for_oem_candidate_lookup_needs_manual_confirmation_count": sum(
                1 for item in work_items if item["status"] == "ready_for_oem_candidate_lookup_needs_manual_confirmation"
            ),
            "ready_for_live_auto_lookup_count": sum(
                1 for item in work_items if item["status"] == "ready_for_live_auto_oem_cross_and_price_lookup"
            ),
            "needs_identity_confirmation_count": sum(
                1 for item in work_items if item["status"] == "needs_identity_confirmation_before_parts_search"
            ),
            "needs_part_intent_clarification_count": sum(
                1 for item in work_items if item["status"] == "needs_part_intent_clarification_before_catalog_search"
            ),
            "needs_part_position_clarification_count": sum(
                1 for item in work_items if item["status"] == "needs_part_position_clarification_before_catalog_search"
            ),
            "needs_vin_or_frame_count": sum(1 for item in work_items if item["status"] == "needs_vin_or_frame"),
            "needs_identity_confirmation_resolver_count": sum(1 for item in work_items if item["status"] == "needs_identity_confirmation"),
            "needs_part_clarification_resolver_count": sum(1 for item in work_items if item["status"] == "needs_part_clarification"),
            "needs_partsapi_category_mapping_count": sum(1 for item in work_items if item["status"] == "needs_partsapi_category_mapping"),
            "ready_for_live_oem_candidate_lookup_count": sum(1 for item in work_items if item["status"] == "ready_for_live_oem_candidate_lookup"),
            "oem_candidates_found_needs_manual_confirmation_count": sum(
                1 for item in work_items if item["status"] == "oem_candidates_found_needs_manual_confirmation"
            ),
            "no_oem_candidate_found_needs_manual_epc_count": sum(
                1 for item in work_items if item["status"] == "no_oem_candidate_found_needs_manual_epc"
            ),
            "confirmed_for_manual_crm_writeback_count": sum(1 for item in work_items if item["status"] == "confirmed_for_manual_crm_writeback"),
        },
        "next_decision": (
            "No VIN/frame items supplied; add at least one item before planning OEM and supplier lookup."
            if count == 0
            else "Configure live OEM catalog and supplier credentials to move from manual-ready work orders to full automated lookup."
            if full_auto_count < count
            else "Live automated lookup is ready for every item; execute read-only lookups before CRM writeback."
        ),
        "items": work_items,
        "privacy": benchmark["privacy"],
    }
