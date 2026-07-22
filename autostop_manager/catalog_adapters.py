from __future__ import annotations

from dataclasses import asdict, dataclass
import os
from typing import Any
from urllib.parse import quote_plus

from .config import load_runtime_env
from .parts_intent import normalize_part_intent
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
        docs_url="docs/agent/vehicle_identity_playbook.md",
    ),
    CatalogProvider(
        source_id="partsapi_ru",
        name="PARTSAPI.RU",
        stage="catalog_cross",
        access_mode="api_key",
        env_names=("PARTSAPI_BASE_URL",),
        env_any_groups=(
            ("PARTSAPI_KEY",),
            ("PARTSAPI_VINDECODE_KEY",),
            ("PARTSAPI_VINDECODE_OE_KEY",),
            ("PARTSAPI_PARTS_BY_VIN_KEY",),
            ("PARTSAPI_OE_APPLICABILITY_KEY",),
            ("PARTSAPI_CROSSES_KEY",),
            ("PARTSAPI_CROSSES_WITH_BRAND_KEY",),
            ("PARTSAPI_CROSSES_TITLE_KEY",),
            ("PARTSAPI_ARTICLE_CROSSES_KEY",),
            ("PARTSAPI_SEARCH_ARTICLES_KEY",),
            ("PARTSAPI_GET_ENGINE_KEY",),
            ("PARTSAPI_SEARCH_TREE_KEY",),
            ("PARTSAPI_ARTICLES_KEY",),
            ("PARTSAPI_ARTICLE_KEY",),
            ("PARTSAPI_ARTICLE_CRITERIA_KEY",),
        ),
        capabilities=(
            "vin_decode",
            "vin_decode_oe",
            "parts_by_vin",
            "oe_applicability",
            "search_articles",
            "crosses",
            "article_crosses",
            "crosses_with_brand",
            "crosses_title",
            "engine_info",
            "search_tree",
            "articles",
            "article",
            "article_criteria",
        ),
        priority="high",
        role="Primary MVP candidate for VIN/OE decode, applicability, and cross/analog checks.",
        limits="Needs account/API key; not a confirmed procurement stock source unless supplier prices are connected.",
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
        source_id="parts_catalogs_api",
        name="Parts-Catalogs API",
        stage="oem_catalog",
        access_mode="api_key",
        env_names=("PARTS_CATALOGS_API_KEY", "PARTS_CATALOGS_BASE_URL"),
        capabilities=("car_info_by_vin_frame", "catalog_groups", "oem_parts", "diagrams", "schema_parts"),
        priority="high",
        role="Primary OEM catalog candidate for exact vehicle profile and OEM group/part lookup.",
        limits="Commercial subscription; brand/market coverage must be validated on AutoStop test VIN/frame set.",
        docs_url="https://www.parts-catalogs.com/doc/us/introduction.htm",
    ),
    CatalogProvider(
        source_id="vin17_api",
        name="17VIN API",
        stage="oem_catalog",
        access_mode="account_token",
        env_names=(),
        env_any_groups=(("VIN17_ACCOUNT", "VIN17_SECRET"),),
        capabilities=("vin_decode", "common_parts_by_vin", "part_search_by_vin", "oe_search", "replacement_numbers"),
        priority="medium",
        role="Second-source EPC candidate, useful for common-wear parts and replacement chains.",
        limits="Commercial account; confirm language, latency, market coverage, and legal terms.",
        docs_url="https://en.17vin.com/doc.html",
    ),
    CatalogProvider(
        source_id="autopoisk",
        name="AUTOPOISK",
        stage="catalog_cross",
        access_mode="subscription_or_manual",
        env_names=("AUTOPOISK_TOKEN",),
        capabilities=("vin_frame_identification", "oem_catalog", "cross_tab", "supplier_statistics"),
        priority="medium",
        role="Professional manual/semiautomatic EPC and cross verification candidate.",
        limits="Business subscription/demo required; do not assume API until terms are confirmed.",
        docs_url="https://autopoisk.su/en",
        manual_allowed=True,
    ),
    CatalogProvider(
        source_id="partslink24_or_oem_epc",
        name="partslink24 or brand EPC",
        stage="oem_catalog",
        access_mode="manual_subscription",
        env_names=(),
        capabilities=("brand_epc_by_vin", "options_pr_codes", "production_date", "genuine_parts"),
        priority="high",
        role="Dealer-grade route for VAG, Mercedes, BMW, and other European VINs.",
        limits="Manual subscription/login outside Git; no password storage; use only authorized access.",
        docs_url="https://www.partslink24.com/en",
        manual_allowed=True,
    ),
    CatalogProvider(
        source_id="rossko",
        name="ROSSKO",
        stage="procurement_price",
        access_mode="account_api",
        env_names=(),
        env_any_groups=(("ROSSKO_KEY1", "ROSSKO_KEY2"), ("ROSSKO_API_KEY1", "ROSSKO_API_KEY2")),
        capabilities=("supplier_search", "stock", "procurement_price", "delivery", "order_status"),
        priority="high",
        role="Krasnoyarsk-first procurement price/stock source after account keys are available.",
        limits="Do not order without explicit owner command; analog settings and no-stock behavior need testing.",
        docs_url="https://api.rossko.ru/",
    ),
    CatalogProvider(
        source_id="autoeuro_api",
        name="AutoEuro API",
        stage="procurement_price",
        access_mode="api_key",
        env_names=("AUTOEURO_API_KEY",),
        capabilities=("brand_article_search", "stock", "delivery", "order_status"),
        priority="high",
        role="Supplier API for price/stock confirmation after account activation.",
        limits="Daily limits for new accounts; broad price-list export may be better for bulk проценка.",
        docs_url="https://api.autoeuro.ru/doc/v2",
    ),
    CatalogProvider(
        source_id="zzap",
        name="ZZap",
        stage="market_price",
        access_mode="partner_or_manual",
        env_names=("ZZAP_API_KEY",),
        capabilities=("retail_market_range", "seller_offers", "replacement_visibility"),
        priority="medium",
        role="RF market benchmark and replacement visibility.",
        limits="Benchmark/retail source, not confirmed закупка unless contracted supplier result is visible.",
        docs_url="https://www.zzap.ru/",
        manual_allowed=True,
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
        source_id="armtek",
        name="Armtek",
        stage="procurement_price",
        access_mode="account_or_etp",
        env_names=("ARMTEK_LOGIN", "ARMTEK_PASSWORD"),
        capabilities=("brand_article_search", "stock", "lead_time", "procurement_price"),
        priority="high",
        role="B2B procurement and stock/lead-time benchmark candidate.",
        limits="Use only approved account/API/export route; do not scrape private cabinet.",
        docs_url="https://etp.armtek.ru/",
    ),
    CatalogProvider(
        source_id="autopiter",
        name="Autopiter",
        stage="procurement_price",
        access_mode="account_webservice",
        env_names=("AUTOPITER_USER_ID", "AUTOPITER_PASSWORD"),
        capabilities=("brand_article_search", "brand_disambiguation", "stock", "delivery"),
        priority="medium",
        role="Russia-wide wholesale/order benchmark candidate.",
        limits="Wholesale terms require account; public website is retail/benchmark only.",
        docs_url="https://autopiter.ru/opt",
    ),
    CatalogProvider(
        source_id="emex",
        name="Emex",
        stage="procurement_price",
        access_mode="account_webservice_ip_whitelist",
        env_names=("EMEX_LOGIN", "EMEX_PASSWORD"),
        capabilities=("brand_article_search", "stock", "lead_time", "procurement_price", "delivery_probability"),
        priority="medium",
        role="Russia-wide supplier benchmark and procurement candidate through official SOAP web-service after account and IP whitelist.",
        limits="Requires Emex account, service access request, and whitelisted server IP; do not scrape private cabinet, basket, or /api pages.",
        docs_url="http://wsdoc.emex.ru/FindDetailAdv5.html",
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
        providers.append(
            {
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
        )
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
    return [
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
    cross_providers = _pick_configured(_providers_for_stage("catalog_cross"))
    aftermarket_providers = _pick_configured(_providers_for_stage("aftermarket_catalog"))
    procurement_providers = _pick_configured(_providers_for_stage("procurement_price"))
    market_providers = _pick_configured(_providers_for_stage("market_price"))

    oem_capable_source_ids = {"parts_catalogs_api", "vin17_api", "partsapi_ru"}
    live_oem = [
        provider
        for provider in oem_providers + cross_providers
        if provider["live_callable_now"] and provider["source_id"] in oem_capable_source_ids
    ]
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
        blockers.append(
            {
                "stage": "oem_catalog",
                "reason": "No live VIN/frame-specific OEM catalog API is configured.",
                "missing_env": missing_env,
                "missing_env_names": missing_env,
            }
        )
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
                "step": "find_oem_candidates",
                "providers": [provider["source_id"] for provider in oem_providers],
                "acceptance": "VIN/frame-specific catalog returns OEM candidates with group/position/production evidence",
                "part_search_terms": part_profile.get("catalog_search_terms", [])[:8],
                "critical_vehicle_fields": part_profile.get("critical_vehicle_fields", []),
            },
            {
                "step": "verify_applicability_and_crosses",
                "providers": [provider["source_id"] for provider in cross_providers],
                "acceptance": "OEM applicability and cross/analog confidence are separated; title-match crosses stay unconfirmed",
                "fitment_caveats": part_profile.get("fitment_caveats", []),
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
