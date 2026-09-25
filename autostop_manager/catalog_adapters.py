from __future__ import annotations

from dataclasses import asdict, dataclass
import os
from typing import Any
from urllib.parse import quote_plus

from .catalog_clients import PARTSAPI_METHOD_KEY_ENV_NAMES, PARTSAPI_OPERATIONS, partsapi_operation_status
from .config import load_runtime_env
from .parts_intent import normalize_part_intent
from .vin_sources import AMAYAMA_SOURCE_ID, PARTSOUQ_SOURCE_ID, PUBLIC_CATALOG_SOURCE_ALIASES
from .vin_lookup import classify_identifier


@dataclass(frozen=True)
class CatalogProvider:
    source_id: str
    name: str
    stage: str
    access_mode: str
    env_names: tuple[str, ...]
    capabilities: tuple[str, ...]
    priority: str
    role: str
    limits: str
    docs_url: str
    manual_allowed: bool = False
    env_any_groups: tuple[tuple[str, ...], ...] = ()
    aliases: tuple[str, ...] = ()


PROVIDERS: tuple[CatalogProvider, ...] = (
    CatalogProvider(
        source_id="nhtsa_vpic",
        name="NHTSA vPIC API",
        stage="identity",
        access_mode="public_api",
        env_names=(),
        capabilities=("vin_decode_basic", "wmi_decode", "model_year_hint"),
        priority="baseline",
        role="Free baseline for VIN/WMI decode and sanity checks.",
        limits="Not an EPC; often partial for ROW, Japan, Russia/CIS, China, and Europe-market vehicles.",
        docs_url="https://vpic.nhtsa.dot.gov/api/Home/Index",
    ),
    CatalogProvider(
        source_id="local_platform_rules",
        name="AutoStop local platform rules",
        stage="identity",
        access_mode="local_rules",
        env_names=(),
        capabilities=("row_jdm_platform_hint", "frame_query_hint", "crm_conflict_detection"),
        priority="baseline",
        role="Local hints for common AutoStop ROW/JDM patterns and CRM identity conflicts.",
        limits="Rules are not VIN-specific EPC confirmation; cannot prove options or OEM parts alone.",
        docs_url=".agents/skills/manage-autostop-store/SKILL.md",
    ),
    CatalogProvider(
        source_id="partsapi_ru",
        name="PARTSAPI.RU",
        stage="catalog_cross",
        access_mode="api_key",
        env_names=("PARTSAPI_BASE_URL",),
        env_any_groups=tuple((name,) for name in sorted(set(PARTSAPI_METHOD_KEY_ENV_NAMES.values()))),
        capabilities=tuple(PARTSAPI_OPERATIONS),
        priority="high",
        role="VIN identity, TecDoc article, part-name/cross, AUTONORMS labor-time, and fluid-volume lookup.",
        limits="TecDoc articles are candidates, not VIN-specific OEM EPC proof or confirmed procurement stock.",
        docs_url="https://partsapi.ru/docs",
    ),
    CatalogProvider(
        source_id="mann_filter_catalog",
        name="MANN-FILTER Catalog",
        stage="aftermarket_catalog",
        access_mode="public_api",
        env_names=(),
        capabilities=("filter_part_search", "oe_number_search", "comparison_numbers", "product_details"),
        priority="medium",
        role="Live public aftermarket filter catalog for part/OE search and comparison references.",
        limits="Brand-scope filter catalog only; not a VIN-specific OEM EPC and not a procurement price source.",
        docs_url="https://www.mann-filter.com/us-en/catalog.html",
    ),
    CatalogProvider(
        source_id="denso_aftermarket_catalog",
        name="DENSO Aftermarket Catalog",
        stage="aftermarket_catalog",
        access_mode="public_api",
        env_names=(),
        capabilities=("part_number_search", "oe_number_search", "product_details", "vin_search_public_site"),
        priority="medium",
        role="Live public DENSO aftermarket catalog for DENSO/OE number search and product detail checks.",
        limits="Brand-scope aftermarket catalog; VIN search is DENSO fitment only and does not replace OEM EPC confirmation.",
        docs_url="https://www.denso-am.eu/catalog/vin",
    ),
    CatalogProvider(
        source_id=PARTSOUQ_SOURCE_ID,
        name="PartSouq public catalog",
        stage="oem_catalog",
        access_mode="public_site_manual",
        env_names=(),
        capabilities=(
            "vin_or_frame_entry",
            "vehicle_parameter_navigation",
            "part_name_diagram_search",
            "oem_part_number_candidate",
            "diagram_link_capture",
            "applicability_condition_capture",
        ),
        priority="medium",
        role="Free public route for preliminary JDM/Asian catalog diagrams and OEM-number candidates after the vehicle identity is known.",
        limits="Manual public-site use only. Do not automate login, cart, checkout, private/mobile endpoints, or bypass JavaScript, cookie, rate-limit, or anti-bot checks. Capture the diagram/page link and model/period/engine/transmission/position evidence; a returned number is preliminary until fitment is independently checked.",
        docs_url="https://partsouq.com/en/",
        manual_allowed=True,
        aliases=PUBLIC_CATALOG_SOURCE_ALIASES[PARTSOUQ_SOURCE_ID],
    ),
    CatalogProvider(
        source_id=AMAYAMA_SOURCE_ID,
        name="Amayama public catalog",
        stage="oem_catalog",
        access_mode="public_site_manual",
        env_names=(),
        capabilities=(
            "vin_or_frame_entry",
            "vehicle_parameter_navigation",
            "part_name_diagram_search",
            "oem_part_number_candidate",
            "diagram_link_capture",
            "applicability_condition_capture",
        ),
        priority="medium",
        role="Free public route for preliminary Japanese catalog diagrams, OEM-number candidates, and visible applicability conditions.",
        limits="Manual public-site use only. Do not automate login, cart, checkout, private/mobile endpoints, or bypass JavaScript, cookie, rate-limit, or anti-bot checks. Treat the site's compatibility information as a reference and verify model code, period, engine, transmission, market, and position before order.",
        docs_url="https://www.amayama.com/en/genuine-catalogs",
        manual_allowed=True,
        aliases=PUBLIC_CATALOG_SOURCE_ALIASES[AMAYAMA_SOURCE_ID],
    ),
    CatalogProvider(
        source_id="euroauto_catalog",
        name="EuroAuto public catalog",
        stage="market_price",
        access_mode="public_site_manual",
        env_names=(),
        capabilities=(
            "part_number_search",
            "vin_search_public_site",
            "used_parts",
            "contract_parts",
            "new_parts",
            "market_price_reference",
        ),
        priority="medium",
        role="Read-only catalog route for new, used, and contract-part discovery; use as a market reference after OEM/fitment confirmation.",
        limits="EuroAuto is distinct from AutoEuro. No buyer API has been approved for AutoStop: use only the public catalog, do not automate login, basket, checkout, messages, private/mobile endpoints, or bypass anti-bot protection. Verify listing, condition, delivery, warranty, and return terms live.",
        docs_url="https://krasnoyarsk.euroauto.ru/",
        manual_allowed=True,
    ),
    CatalogProvider(
        source_id="exist",
        name="Exist",
        stage="procurement_price",
        access_mode="public_site_read_only",
        env_names=(),
        capabilities=(
            "brand_article_search",
            "retail_price_benchmark",
            "lead_time",
            "replacements",
            "catalog_disambiguation",
        ),
        priority="medium",
        role="Public read-only retail benchmark and catalog disambiguation route for exact article checks in Krasnoyarsk office 905.",
        limits="Use as public_retail_reference only; do not use login, cabinet, basket, orders, private APIs, or raw HTML as procurement confirmation.",
        docs_url="https://s.exist.ru/xml/osd.xml",
        manual_allowed=True,
    ),
)


def _env_configured(
    names: tuple[str, ...], any_groups: tuple[tuple[str, ...], ...] = ()
) -> tuple[bool, list[str], list[str], list[list[str]]]:
    present = [name for name in names if os.getenv(name)]
    missing = [name for name in names if not os.getenv(name)]
    missing_groups = [[name for name in group if not os.getenv(name)] for group in any_groups]
    if any_groups:
        group_configured = any(not group_missing for group_missing in missing_groups)
        group_names = [name for group in any_groups for name in group]
        group_present = [name for name in group_names if os.getenv(name)]
        group_missing_flat = sorted({name for group in missing_groups for name in group})
        effective_group_missing = [] if group_configured else group_missing_flat
        effective_missing_groups = [] if group_configured else missing_groups
        return (
            len(missing) == 0 and group_configured,
            sorted(set(present + group_present)),
            sorted(set(missing + effective_group_missing)),
            effective_missing_groups,
        )
    return (len(missing) == 0, present, missing, missing_groups)


def catalog_provider_status(*, stage: str | None = None) -> dict[str, Any]:
    load_runtime_env()
    providers = []
    for provider in PROVIDERS:
        if stage and provider.stage != stage:
            continue
        configured, present, missing, missing_groups = _env_configured(provider.env_names, provider.env_any_groups)
        if not provider.env_names:
            configured = bool(provider.env_any_groups and configured) or provider.access_mode in {
                "public_api",
                "public_site_read_only",
                "public_site_manual",
                "local_rules",
            }
        row = {
            **asdict(provider),
            "env_names": list(provider.env_names),
            "env_any_groups": [list(group) for group in provider.env_any_groups],
            "capabilities": list(provider.capabilities),
            "configured": configured,
            "present_env_names": present,
            "missing_env_names": missing,
            "missing_env_groups": missing_groups,
            "live_callable_now": configured
            and provider.access_mode
            not in {
                "manual_subscription",
                "subscription_or_manual",
                "partner_or_manual",
                "public_site_manual",
                "local_rules",
            },
        }
        if provider.source_id == "partsapi_ru":
            operation_status = {
                operation: partsapi_operation_status(operation) for operation in sorted(PARTSAPI_OPERATIONS)
            }
            configured_operations = [
                operation for operation, status in operation_status.items() if status.get("configured")
            ]
            row.update(
                {
                    "authorization_status": "unverified" if configured_operations else "not_configured",
                    "readiness_basis": "configuration_only",
                    "live_callable_now": False,
                    "configured_operations": configured_operations,
                }
            )
            row["operation_status"] = operation_status
            row["live_operations"] = []
        providers.append(row)
    stage_matrix = _provider_stage_matrix(providers)
    return {
        "ok": True,
        "stage": stage,
        "providers": providers,
        "stage_matrix": stage_matrix,
        "configured_count": sum(1 for provider in providers if provider["configured"]),
        "live_callable_count": sum(1 for provider in providers if provider["live_callable_now"]),
        "missing_provider_ids": [provider["source_id"] for provider in providers if not provider["configured"]],
    }


def _provider_stage_matrix(providers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    stage_order = [
        "identity",
        "oem_catalog",
        "catalog_cross",
        "aftermarket_catalog",
        "procurement_price",
        "market_price",
    ]
    stage_labels = {
        "identity": "identity",
        "oem_catalog": "OEM",
        "catalog_cross": "cross",
        "aftermarket_catalog": "aftermarket",
        "procurement_price": "procurement",
        "market_price": "market benchmark",
    }
    matrix: list[dict[str, Any]] = []
    by_stage = {stage: [provider for provider in providers if provider["stage"] == stage] for stage in stage_order}
    for provider_stage in stage_order:
        stage_providers = by_stage.get(provider_stage, [])
        if not stage_providers:
            continue
        matrix.append(
            {
                "stage": provider_stage,
                "label": stage_labels[provider_stage],
                "provider_ids": [provider["source_id"] for provider in stage_providers],
                "configured_count": sum(1 for provider in stage_providers if provider["configured"]),
                "live_callable_count": sum(1 for provider in stage_providers if provider["live_callable_now"]),
                "missing_provider_ids": [
                    provider["source_id"] for provider in stage_providers if not provider["configured"]
                ],
            }
        )
    return matrix


def _providers_for_stage(stage: str) -> list[dict[str, Any]]:
    return catalog_provider_status(stage=stage)["providers"]


def _pick_configured(providers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [provider for provider in providers if provider["configured"] or provider["manual_allowed"]]


def _redact_identifier(identifier: str) -> dict[str, Any]:
    compact = "".join(str(identifier or "").split()).upper()
    if len(compact) <= 6:
        display = compact[:2] + "***" if compact else ""
    else:
        display = f"{compact[:3]}***{compact[-3:]}"
    return {"display": display, "length": len(compact), "prefix": compact[:3] if len(compact) >= 3 else compact}


def _manual_public_search_queries(
    *,
    requested_part: str,
    part_profile: dict[str, Any],
    vehicle_profile: dict[str, Any],
    city: str,
) -> list[dict[str, Any]]:
    vehicle_bits = [
        vehicle_profile.get("make"),
        vehicle_profile.get("model") or vehicle_profile.get("model_family"),
        vehicle_profile.get("model_year"),
        vehicle_profile.get("engine"),
    ]
    vehicle_text = " ".join(str(value).strip() for value in vehicle_bits if value not in (None, ""))
    part_terms = part_profile.get("catalog_search_terms") or [requested_part]
    primary_part = str(part_terms[0] if part_terms else requested_part).strip()
    query = " ".join(part for part in [vehicle_text, primary_part, city] if part).strip()
    compact_query = quote_plus(query)
    catalog_capture_fields = [
        "OEM number and supersession, if shown",
        "direct diagram or part-page URL",
        "catalog group/name and quantity",
        "model code, production period, engine, transmission, market, and side/position conditions",
    ]
    return [
        {
            "source_id": PARTSOUQ_SOURCE_ID,
            "aliases": list(PUBLIC_CATALOG_SOURCE_ALIASES[PARTSOUQ_SOURCE_ID]),
            "role": "Public preliminary OEM-candidate and diagram route for VIN, frame, or vehicle-parameter lookup.",
            "query": query,
            "url": "https://partsouq.com/en/",
            "needs": "Enter the original VIN/frame only in the interactive catalog, or select the exact vehicle parameters; capture the evidence below. If access is challenged or empty, continue to the next source without bypassing it.",
            "capture_fields": catalog_capture_fields,
        },
        {
            "source_id": AMAYAMA_SOURCE_ID,
            "aliases": list(PUBLIC_CATALOG_SOURCE_ALIASES[AMAYAMA_SOURCE_ID]),
            "role": "Public preliminary Japanese OEM-candidate and diagram route for VIN, frame, article, or vehicle-parameter lookup.",
            "query": query,
            "url": "https://www.amayama.com/en/genuine-catalogs",
            "needs": "Enter the original VIN/frame only in the interactive catalog, or select exact model/year/engine/transmission; capture the evidence below. If access is challenged or empty, continue to the next source without bypassing it.",
            "capture_fields": catalog_capture_fields,
        },
        {
            "source_id": "zzap_manual",
            "role": "RF market benchmark and replacement visibility; not procurement proof.",
            "query": query,
            "url": f"https://www.zzap.ru/public/search.aspx#rawdata={compact_query}",
            "needs": "exact OEM or selected article before high-confidence fitment/pricing",
        },
        {
            "source_id": "drom_parts_manual",
            "role": "Local/used/contract part search in Krasnoyarsk.",
            "query": query,
            "url": f"https://baza.drom.ru/krasnoyarsk/sell_spare_parts/?query={compact_query}",
            "needs": "photo/article/seller confirmation; do not use as OEM proof",
        },
        {
            "source_id": "euroauto_catalog_manual",
            "role": "Public EuroAuto catalog for used, contract, and new-part market alternatives.",
            "query": query,
            "url": "https://krasnoyarsk.euroauto.ru/",
            "needs": "enter the OEM/article or VIN in the public catalog; verify live listing, condition, delivery, warranty, and return terms",
        },
        {
            "source_id": "avito_parts_manual",
            "role": "Local marketplace sanity check for urgent used/contract parts.",
            "query": query,
            "url": f"https://www.avito.ru/krasnoyarsk/zapchasti_i_aksessuary?q={compact_query}",
            "needs": "seller confirmation, condition, kit completeness, and return terms",
        },
    ]


def build_oem_parts_provider_plan(
    *,
    identifier: str,
    requested_part: str,
    vehicle_identity: dict[str, Any] | None = None,
    city: str = "Красноярск",
) -> dict[str, Any]:
    classification = classify_identifier(identifier)
    part_profile = normalize_part_intent(requested_part)
    identity = vehicle_identity or {}
    profile = identity.get("vehicle_profile") or {}
    confidence_label = identity.get("confidence_label") or "unknown"
    strict_identity_ready = confidence_label == "high" and not any(
        conflict.get("severity") == "high" for conflict in identity.get("conflicts", [])
    )
    readiness = identity.get("parts_lookup_readiness") or {}
    identity_ready = bool(
        readiness.get("ready_for_oem_candidate_lookup", readiness.get("ready_for_oem_lookup", strict_identity_ready))
    )
    writeback_ready = bool(readiness.get("ready_for_crm_writeback", strict_identity_ready))

    identity_providers = _pick_configured(_providers_for_stage("identity"))
    oem_providers = _pick_configured(_providers_for_stage("oem_catalog"))
    all_cross_providers = _providers_for_stage("catalog_cross")
    cross_providers = _pick_configured(all_cross_providers)
    aftermarket_providers = _pick_configured(_providers_for_stage("aftermarket_catalog"))
    procurement_providers = _pick_configured(_providers_for_stage("procurement_price"))
    market_providers = _pick_configured(_providers_for_stage("market_price"))

    partsapi_provider = next(
        (provider for provider in all_cross_providers if provider["source_id"] == "partsapi_ru"), None
    )
    partsapi_operation_statuses = dict(partsapi_provider.get("operation_status") or {}) if partsapi_provider else {}
    partsapi_tecdoc_operations = ("vin_decode", "search_tree", "articles")
    partsapi_configured_tecdoc_operations = [
        operation
        for operation in partsapi_tecdoc_operations
        if bool((partsapi_operation_statuses.get(operation) or {}).get("configured"))
    ]
    partsapi_tecdoc_chain_configured = len(partsapi_configured_tecdoc_operations) == len(partsapi_tecdoc_operations)
    live_oem: list[dict[str, Any]] = []
    oem_candidate_providers = list(oem_providers)
    live_aftermarket = [provider for provider in aftermarket_providers if provider["live_callable_now"]]
    live_price_references = [provider for provider in procurement_providers if provider["live_callable_now"]]
    live_procurement = [
        provider
        for provider in live_price_references
        if provider["access_mode"] != "public_site_read_only"
        and "retail_price_benchmark" not in provider.get("capabilities", [])
    ]

    blockers: list[dict[str, Any]] = []
    if not live_oem:
        missing_env = sorted(
            {
                name
                for provider in _providers_for_stage("oem_catalog") + _providers_for_stage("catalog_cross")
                for name in provider["missing_env_names"]
            }
        )
        blocker = {
            "stage": "oem_catalog",
            "reason": "No live VIN/frame-specific OEM catalog availability is verified.",
            "missing_env": missing_env,
            "missing_env_names": missing_env,
        }
        if partsapi_tecdoc_chain_configured:
            blocker["partsapi_scope"] = "TecDoc article candidates; exact OEM applicability requires an OEM EPC."
        blockers.append(blocker)
    if not live_procurement:
        missing_env = sorted(
            {name for provider in _providers_for_stage("procurement_price") for name in provider["missing_env_names"]}
        )
        blockers.append(
            {
                "stage": "procurement_price",
                "reason": "No live supplier price/stock API is configured.",
                "missing_env": missing_env,
                "missing_env_names": missing_env,
            }
        )

    return {
        "ok": True,
        "identifier": {
            "redacted": _redact_identifier(identifier),
            "kind": classification.kind,
            "market_hint": classification.market_hint,
            "raw_identifier_is_sensitive": True,
        },
        "requested_part": requested_part,
        "requested_part_profile": part_profile,
        "city": city,
        "vehicle_profile": {
            key: profile.get(key)
            for key in [
                "make",
                "model",
                "model_family",
                "platform",
                "model_year",
                "engine",
                "transmission",
                "drivetrain",
                "market",
            ]
            if profile.get(key) not in (None, "")
        },
        "identity_confidence": confidence_label,
        "live_capability": {
            "identity_ready_for_parts": identity_ready,
            "identity_ready_for_oem_candidate_lookup": identity_ready,
            "identity_ready_for_crm_writeback": writeback_ready,
            "live_oem_catalog_available": bool(live_oem),
            "live_oem_candidate_lookup_available": False,
            "live_oem_applicability_available": False,
            "configured_oem_candidate_lookup_available": False,
            "configured_oem_applicability_available": False,
            "configured_tecdoc_article_lookup_available": partsapi_tecdoc_chain_configured,
            "live_tecdoc_article_lookup_available": False,
            "partsapi_authorization_status": (
                partsapi_provider.get("authorization_status") if partsapi_provider else "not_configured"
            ),
            "partsapi_readiness_basis": (
                partsapi_provider.get("readiness_basis") if partsapi_provider else "configuration_only"
            ),
            "partsapi_tecdoc_operations": {
                operation: partsapi_operation_statuses.get(operation, {}) for operation in partsapi_tecdoc_operations
            },
            "partsapi_configured_tecdoc_operations": partsapi_configured_tecdoc_operations,
            "partsapi_oem_operations": {},
            "partsapi_configured_oem_operations": [],
            "partsapi_live_oem_operations": [],
            "live_aftermarket_catalog_available": bool(live_aftermarket),
            "live_price_reference_available": bool(live_price_references),
            "live_public_retail_reference_available": any(
                provider["source_id"] == "exist" for provider in live_price_references
            ),
            "live_procurement_available": bool(live_procurement),
            "can_complete_full_auto_lookup_now": writeback_ready and bool(live_oem) and bool(live_procurement),
        },
        "pipeline": [
            {
                "step": "decode_vehicle_identity",
                "providers": [provider["source_id"] for provider in identity_providers],
                "acceptance": "identity is high confidence or uncertainty is carried into quote matrix",
            },
            {
                "step": "lookup_tecdoc_articles",
                "providers": ["partsapi_ru"] if partsapi_provider and partsapi_tecdoc_chain_configured else [],
                "acceptance": "VINdecode carId, a selected getSearchTree strId, and getArticles return candidate articles; verify fitment separately",
            },
            {
                "step": "find_oem_candidates",
                "providers": list(dict.fromkeys(provider["source_id"] for provider in oem_candidate_providers)),
                "acceptance": "VIN/frame-specific catalog returns OEM candidates with group/position/production evidence",
                "part_search_terms": part_profile.get("catalog_search_terms", [])[:8],
                "critical_vehicle_fields": part_profile.get("critical_vehicle_fields", []),
            },
            {
                "step": "verify_applicability_and_crosses",
                "providers": [provider["source_id"] for provider in cross_providers],
                "acceptance": "OEM applicability and cross/analog confidence are separated; title-match crosses stay unconfirmed",
            },
            {
                "step": "lookup_public_aftermarket_catalogs",
                "providers": [provider["source_id"] for provider in aftermarket_providers],
                "acceptance": "Brand-scope public catalog data can enrich crosses/details, but cannot prove VIN-specific OEM fitment alone",
            },
            {
                "step": "quote_procurement_price",
                "providers": [provider["source_id"] for provider in procurement_providers],
                "acceptance": f"{city} stock/procurement result or explicit needs-confirmation",
            },
            {
                "step": "quote_market_price",
                "providers": [provider["source_id"] for provider in market_providers],
                "acceptance": "RF public retail range is separate from procurement and client sale price",
            },
        ],
        "manual_public_search_queries": _manual_public_search_queries(
            requested_part=requested_part,
            part_profile=part_profile,
            vehicle_profile=profile,
            city=city,
        ),
        "blockers": blockers,
        "provider_status": catalog_provider_status(),
        "privacy": {
            "do_not_persist_raw_identifier": True,
            "fixture_rule": "Use synthetic VIN/frame values in tests and docs.",
        },
    }
