from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .catalog_adapters import build_oem_parts_provider_plan, catalog_provider_status
from .catalog_clients import (
    emex_price_lookup,
    exist_price_lookup,
    lookup_oem_catalog_candidates,
    partsapi_catalog_lookup,
    public_aftermarket_catalog_lookup,
    vin17_decode_vehicle,
    vin17_search_part_number_by_vin,
)
from .cleanup_audit import build_cleanup_audit
from .control_center import build_control_report, format_control_report_markdown
from .context import build_agent_brief, prepare_manager_context
from .crm_card_action import prepare_crm_card_action
from .crm_vin_parts import build_crm_vin_parts_lookup_pipeline
from .crm_health import build_crm_health_plan
from .fluid_maintenance import build_fluid_maintenance_plan
from .knowledge_base import (
    audit_knowledge_annotations,
    audit_knowledge_base,
    probe_knowledge_base,
    search_knowledge_base,
    sync_knowledge_base,
)
from .knowledge_intake import build_knowledge_intake_plan
from .memory_curator import audit_memory, curate_memory
from .memory_review import apply_memory_review_item, build_memory_review
from .partsapi_smoke import build_partsapi_vin_smoke_report, select_crm_partsapi_smoke_case
from .partsapi_category_index import (
    build_partsapi_category_index_plan,
    explain_partsapi_category_for_intent,
    search_partsapi_category_index,
    validate_partsapi_category_index,
)
from .provider_smoke import build_provider_smoke_report
from .service_management import build_service_management_plan
from .skill_registry import audit_skill_registry
from .source_catalog import recommend_automotive_sources
from .storage import ManagerMemoryStore
from .system_audit import build_system_audit
from .vehicle_identity import decode_vehicle_identities, decode_vehicle_identity
from .vin_parts_benchmark import benchmark_vin_parts_lookup
from .vin_parts_work_order import build_vin_parts_work_order
from .vin_oem_resolver import resolve_vin_oem_parts
from .vin_lookup import lookup_original_parts
from .work_pricing import estimate_repair_work_cost


def _tags(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


def _print_json(payload: dict[str, Any]) -> None:
    stdout_reconfigure = getattr(sys.stdout, "reconfigure", None)
    if stdout_reconfigure is not None:
        stdout_reconfigure(encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _print_text(payload: str) -> None:
    stdout_reconfigure = getattr(sys.stdout, "reconfigure", None)
    if stdout_reconfigure is not None:
        stdout_reconfigure(encoding="utf-8")
    print(payload, end="" if payload.endswith("\n") else "\n")


def _write_output(raw_path: str | None, payload: str) -> None:
    if not raw_path:
        return
    path = Path(raw_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8")


def _json_object(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {"raw": raw}
    return value if isinstance(value, dict) else {"value": value}


def _json_value(raw: str | None, *, option_name: str) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        message = f"{option_name} must be valid JSON: {exc}"
        raise SystemExit(message) from exc


def _json_file(raw_path: str | None, *, option_name: str) -> Any:
    if not raw_path:
        return None
    path = Path(raw_path)
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        message = f"{option_name} must point to a valid JSON file: {exc}"
        raise SystemExit(message) from exc


def _json_dict_arg(raw: str | None, *, option_name: str) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        message = f"{option_name} must be valid JSON: {exc}"
        raise SystemExit(message) from exc
    if not isinstance(value, dict):
        message = f"{option_name} must be a JSON object"
        raise SystemExit(message)
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="autostop-manager", description="AutoStop manager memory CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    remember = sub.add_parser("remember", help="Store a note or fact")
    remember.add_argument("content")
    remember.add_argument("--kind", choices=["note", "fact"], default="note")
    remember.add_argument("--title", default="")
    remember.add_argument("--category", default="general")
    remember.add_argument("--source", default="codex")
    remember.add_argument("--tags", default="")
    remember.add_argument("--importance", type=float, default=0.5)
    remember.add_argument("--confidence", type=float, default=1.0)
    remember.add_argument("--expires-at", default=None)
    remember.add_argument("--supersedes-id", type=int, default=None)
    remember.add_argument("--sensitivity", default="normal")

    recall = sub.add_parser("recall", help="Search manager memory")
    recall.add_argument("query", nargs="?", default="")
    recall.add_argument("--limit", type=int, default=20)
    recall.add_argument("--kind", choices=["note", "fact", "lesson", "task", "reminder", "journal", "rule"], default=None)
    recall.add_argument("--category", default=None)
    recall.add_argument("--tags", default="")

    learn = sub.add_parser("learn", help="Store a reusable lesson from feedback, praise, failure, or success")
    learn.add_argument("content")
    learn.add_argument("--title", default="")
    learn.add_argument("--applies-to", dest="applies_to", default="general")
    learn.add_argument("--signal", default="manager_observation")
    learn.add_argument("--recommendation", default="")
    learn.add_argument("--avoid", default="")
    learn.add_argument("--importance", type=float, default=0.5)
    learn.add_argument("--confidence", type=float, default=0.7)
    learn.add_argument("--source", default="codex")
    learn.add_argument("--tags", default="")

    lessons = sub.add_parser("lessons", help="Search reusable manager lessons")
    lessons.add_argument("query", nargs="?", default="")
    lessons.add_argument("--limit", type=int, default=20)
    lessons.add_argument("--applies-to", dest="applies_to", default=None)
    lessons.add_argument("--signal", default=None)
    lessons.add_argument("--tags", default="")

    sub.add_parser("memory-map", help="Show memory sections and counts")
    memory_topics = sub.add_parser("memory-topics", help="Show memory categories and tags")
    memory_topics.add_argument("--examples-limit", type=int, default=3)

    memory_context = sub.add_parser("memory-context", help="Build compact memory context for a task")
    memory_context.add_argument("task")
    memory_context.add_argument("--limit", type=int, default=5)

    sub.add_parser("memory-gaps", help="Show sparse or empty memory areas")

    task = sub.add_parser("task", help="Add a manager task")
    task.add_argument("title")
    task.add_argument("--details", default="")
    task.add_argument("--due", default=None)
    task.add_argument("--source", default="codex")
    task.add_argument("--tags", default="")

    remind = sub.add_parser("remind", help="Add a reminder")
    remind.add_argument("title")
    remind.add_argument("--due", required=True)
    remind.add_argument("--details", default="")
    remind.add_argument("--source", default="codex")
    remind.add_argument("--tags", default="")

    journal = sub.add_parser("journal", help="Add a journal event")
    journal.add_argument("event")
    journal.add_argument("--source", default="codex")
    journal.add_argument("--tags", default="")

    today = sub.add_parser("today", help="Return today's manager context")
    today.add_argument("--limit", type=int, default=20)

    prepare_context = sub.add_parser(
        "prepare-context",
        help="Prepare task-specific manager context from memory, rules, command routes, and knowledge base",
    )
    prepare_context.add_argument("query")
    prepare_context.add_argument("--intent", default=None)
    prepare_context.add_argument("--limit", type=int, default=10)

    agent_brief = sub.add_parser(
        "agent-brief",
        help="Return a compact startup package for an agent before broad document reads",
    )
    agent_brief.add_argument("query")
    agent_brief.add_argument("--intent", default=None)
    agent_brief.add_argument("--limit", type=int, default=8)

    prepare_card_action = sub.add_parser(
        "prepare-card-action",
        help="Build a dry-run CRM card update contract without writing CRM",
    )
    prepare_card_action.add_argument("--card-id", required=True)
    prepare_card_action.add_argument("--expected-updated-at", default=None)
    prepare_card_action.add_argument("--description", default=None)
    prepare_card_action.add_argument("--vehicle-profile-json", default=None)
    prepare_card_action.add_argument("--board-summary", default=None)
    prepare_card_action.add_argument("--target-fields", default="")
    prepare_card_action.add_argument("--current-card-json", default=None)
    prepare_card_action.add_argument("--intent", default="board_cleanup")

    lookup = sub.add_parser("lookup-oem", help="Build a VIN/frame OEM lookup dossier for original catalog numbers")
    lookup.add_argument("identifier")
    lookup.add_argument("--model-year", type=int, default=None)
    lookup.add_argument("--make", default=None)
    lookup.add_argument("--part-name", default=None)
    lookup.add_argument("--part-group", default=None)
    lookup.add_argument("--side", default=None)
    lookup.add_argument("--position", default=None)
    lookup.add_argument("--old-part-number", default=None)
    lookup.add_argument("--captured-oem", default=None)
    lookup.add_argument("--captured-source", default=None)
    lookup.add_argument("--captured-supersedes", default=None)
    lookup.add_argument("--captured-note", default=None)

    vehicle_identity = sub.add_parser(
        "decode-vehicle",
        help="Build a source-aware vehicle identity dossier from a VIN/frame/body number and optional CRM context",
    )
    vehicle_identity.add_argument("identifier")
    vehicle_identity.add_argument("--vehicle", default=None)
    vehicle_identity.add_argument("--make", default=None)
    vehicle_identity.add_argument("--model", default=None)
    vehicle_identity.add_argument("--model-year", type=int, default=None)
    vehicle_identity.add_argument("--engine", default=None)
    vehicle_identity.add_argument("--transmission", default=None)
    vehicle_identity.add_argument("--drivetrain", default=None)
    vehicle_identity.add_argument("--market", default=None)
    vehicle_identity.add_argument("--source-confidence", type=float, default=None)
    vehicle_identity.add_argument("--no-live-vpic", action="store_true")

    vehicle_identities = sub.add_parser(
        "decode-vehicles",
        help="Batch decode vehicle identity dossiers from a JSON array of VIN/frame items",
    )
    vehicle_identities.add_argument("--items-json", required=True)
    vehicle_identities.add_argument("--no-live-vpic", action="store_true")
    vehicle_identities.add_argument("--no-vpic-batch", action="store_true")

    catalog_status = sub.add_parser("catalog-status", help="Show configured VIN/OEM/cross/procurement provider readiness")
    catalog_status.add_argument("--stage", default=None)

    provider_smoke = sub.add_parser(
        "provider-smoke",
        help="Run safe provider readiness smoke checks without supplier orders, baskets, or CRM writeback",
    )
    provider_smoke.add_argument("--provider", default="all")
    provider_smoke.add_argument("--mode", choices=["dry-run", "live-readonly"], default="dry-run")

    oem_parts_provider_plan = sub.add_parser(
        "oem-parts-provider-plan",
        help="Build provider readiness plan for VIN/frame -> OEM -> crosses -> procurement price",
    )
    oem_parts_provider_plan.add_argument("identifier")
    oem_parts_provider_plan.add_argument("--part", dest="requested_part", required=True)
    oem_parts_provider_plan.add_argument("--vehicle-identity-json", default=None)
    oem_parts_provider_plan.add_argument("--city", default="Красноярск")

    vin17_decode = sub.add_parser(
        "vin17-decode",
        help="Call or dry-run the 17VIN VIN decoder adapter using VIN17_ACCOUNT/VIN17_SECRET",
    )
    vin17_decode.add_argument("identifier")
    vin17_decode.add_argument("--dry-run", action="store_true")

    vin17_part = sub.add_parser(
        "vin17-search-part",
        help="Call or dry-run 17VIN part-number-by-VIN search after the 3001 decode returns an EPC code",
    )
    vin17_part.add_argument("identifier")
    vin17_part.add_argument("--epc", required=True)
    vin17_part.add_argument("--part-number", required=True)
    vin17_part.add_argument("--match-type", default="exact", choices=["exact", "inexact"])
    vin17_part.add_argument("--dry-run", action="store_true")

    partsapi = sub.add_parser(
        "partsapi-lookup",
        help="Call or dry-run PartsAPI VIN/OE/applicability/cross lookup using PARTSAPI_KEY/PARTSAPI_BASE_URL",
    )
    partsapi.add_argument(
        "--operation",
        required=True,
        choices=[
            "vin_decode",
            "vin_decode_oe",
            "parts_by_vin",
            "oe_applicability",
            "crosses",
            "crosses_with_brand",
            "crosses_title",
            "article_crosses",
            "search_articles",
            "engine_info",
            "search_tree",
            "articles",
            "article",
            "article_criteria",
        ],
    )
    partsapi.add_argument("--identifier", default=None)
    partsapi.add_argument("--part-number", default=None)
    partsapi.add_argument("--article-id", default=None)
    partsapi.add_argument("--brand", default=None)
    partsapi.add_argument(
        "--part-type",
        default=None,
        help="PartsAPI type parameter; default is oem for parts_by_vin, use omit/non-oem to skip type.",
    )
    partsapi.add_argument("--category", default=None)
    partsapi.add_argument("--vehicle-type", default=None)
    partsapi.add_argument("--type-id", default=None)
    partsapi.add_argument("--lang", default=None)
    partsapi.add_argument("--lang-id", type=int, default=None)
    partsapi.add_argument("--timeout", type=float, default=20.0)
    partsapi.add_argument("--max-attempts", type=int, default=1)
    partsapi.add_argument("--dry-run", action="store_true")

    category_index = sub.add_parser("partsapi-category-index", help="Inspect the local PartsAPI numeric category index")
    category_index_sub = category_index.add_subparsers(dest="category_index_command", required=True)
    category_build = category_index_sub.add_parser("build", help="Return the read-only PartsAPI search_tree build plan")
    category_build.add_argument("--live", action="store_true")
    category_build.add_argument("--vehicle-type", default="PC")
    category_build.add_argument("--type-id", default=None)
    category_build.add_argument("--lang-id", type=int, default=16)
    category_build.add_argument("--timeout", type=float, default=20.0)
    category_build.add_argument("--max-attempts", type=int, default=1)
    category_search = category_index_sub.add_parser("search", help="Search the local category index by text")
    category_search.add_argument("--query", required=True)
    category_search.add_argument("--intent", dest="intent_id", default=None)
    category_search.add_argument("--path", default=None)
    category_search.add_argument("--limit", type=int, default=8)
    category_explain = category_index_sub.add_parser("explain", help="Explain category routing for one intent")
    category_explain.add_argument("--intent", dest="intent_id", required=True)
    category_explain.add_argument("--query", default=None)
    category_explain.add_argument("--path", default=None)
    category_validate = category_index_sub.add_parser("validate", help="Validate the tracked category index fixture")
    category_validate.add_argument("--path", default=None)

    public_catalog = sub.add_parser(
        "public-catalog-lookup",
        help="Call public aftermarket catalogs such as MANN-FILTER and DENSO by part/OE number",
    )
    public_catalog.add_argument("--provider", required=True, choices=["mann_filter_catalog", "denso_aftermarket_catalog", "mann", "denso", "all"])
    public_catalog.add_argument("--part-number", required=True)
    public_catalog.add_argument("--page-size", type=int, default=5)
    public_catalog.add_argument("--country", default="europe")
    public_catalog.add_argument("--no-detail", action="store_true")
    public_catalog.add_argument("--dry-run", action="store_true")

    emex_lookup = sub.add_parser(
        "emex-price-lookup",
        help="Call or dry-run official Emex SOAP FindDetailAdv5 price/stock lookup using EMEX_LOGIN/EMEX_PASSWORD",
    )
    emex_lookup.add_argument("--part-number", required=True)
    emex_lookup.add_argument("--brand", default=None, help="Emex makeLogo/brand code, optional")
    emex_lookup.add_argument("--subst-level", default="All", choices=["All", "OriginalOnly"])
    emex_lookup.add_argument(
        "--subst-filter",
        default="None",
        choices=["None", "FilterOriginalAndReplacements", "FilterOriginalAndAnalogs"],
    )
    emex_lookup.add_argument("--delivery-region-type", default="PRI", choices=["PRI", "ALT"])
    emex_lookup.add_argument("--min-delivery-percent", type=int, default=None)
    emex_lookup.add_argument("--max-delivery-days", type=int, default=None)
    emex_lookup.add_argument("--min-quantity", type=int, default=None)
    emex_lookup.add_argument("--max-result-price", type=float, default=None)
    emex_lookup.add_argument("--max-one-detail-offers-count", type=int, default=10)
    emex_lookup.add_argument("--detail-nums-to-load", default="")
    emex_lookup.add_argument("--timeout", type=float, default=20.0)
    emex_lookup.add_argument("--dry-run", action="store_true")

    exist_lookup = sub.add_parser(
        "exist-price-lookup",
        help="Call or dry-run public read-only Exist article price/catalog lookup for retail benchmark",
    )
    exist_lookup.add_argument("--part-number", required=True)
    exist_lookup.add_argument("--brand", default=None)
    exist_lookup.add_argument("--pid", default=None)
    exist_lookup.add_argument("--office-id", type=int, default=905)
    exist_lookup.add_argument("--max-candidates", type=int, default=5)
    exist_lookup.add_argument("--max-offers", type=int, default=10)
    exist_lookup.add_argument("--include-more-offers", action="store_true")
    exist_lookup.add_argument("--timeout", type=float, default=20.0)
    exist_lookup.add_argument("--dry-run", action="store_true")

    oem_catalog_lookup = sub.add_parser(
        "oem-catalog-lookup",
        help="Call or dry-run the three-provider OEM catalog lookup: Parts-Catalogs, PartsAPI, and 17VIN",
    )
    oem_catalog_lookup.add_argument("identifier")
    oem_catalog_lookup.add_argument("--part", dest="requested_part", required=True)
    oem_catalog_lookup.add_argument("--catalog-id", default=None)
    oem_catalog_lookup.add_argument("--car-id", default=None)
    oem_catalog_lookup.add_argument("--group-id", default=None)
    oem_catalog_lookup.add_argument("--epc", default=None)
    oem_catalog_lookup.add_argument("--partsapi-part-type", default="oem")
    oem_catalog_lookup.add_argument("--partsapi-category", default=None)
    oem_catalog_lookup.add_argument("--timeout", type=float, default=20.0)
    oem_catalog_lookup.add_argument("--max-attempts", type=int, default=1)
    oem_catalog_lookup.add_argument("--dry-run", action="store_true")

    partsapi_vin_smoke = sub.add_parser(
        "partsapi-vin-smoke",
        help="Run a bounded read-only PartsAPI VIN/OEM smoke report for one CRM-like item",
    )
    partsapi_vin_smoke.add_argument("--item-json", default=None)
    partsapi_vin_smoke.add_argument("--repair-orders-json", default=None)
    partsapi_vin_smoke.add_argument("--identifier", default=None)
    partsapi_vin_smoke.add_argument("--vehicle", default=None)
    partsapi_vin_smoke.add_argument("--requested-part", default=None)
    partsapi_vin_smoke.add_argument("--partsapi-category", default=None)
    partsapi_vin_smoke.add_argument("--part-type", default="oem")
    partsapi_vin_smoke.add_argument("--max-candidates", type=int, default=3)
    partsapi_vin_smoke.add_argument("--timeout", type=float, default=20.0)
    partsapi_vin_smoke.add_argument("--random-seed", type=int, default=0)
    partsapi_vin_smoke.add_argument("--no-live-vpic", action="store_true")
    partsapi_vin_smoke.add_argument("--dry-run", action="store_true")

    crm_vin_parts = sub.add_parser(
        "crm-vin-parts-plan",
        help="Build the CRM VIN/frame -> OEM -> crosses -> quote -> writeback pipeline for one requested part",
    )
    crm_vin_parts.add_argument("--card-id", default=None)
    crm_vin_parts.add_argument("--part", dest="requested_part", required=True)
    crm_vin_parts.add_argument("--vin", default=None)
    crm_vin_parts.add_argument("--frame", default=None)
    crm_vin_parts.add_argument("--body-number", default=None)
    crm_vin_parts.add_argument("--vehicle", default=None)
    crm_vin_parts.add_argument("--make", default=None)
    crm_vin_parts.add_argument("--model", default=None)
    crm_vin_parts.add_argument("--model-year", type=int, default=None)
    crm_vin_parts.add_argument("--market", default=None)
    crm_vin_parts.add_argument("--engine", default=None)
    crm_vin_parts.add_argument("--transmission", default=None)
    crm_vin_parts.add_argument("--drivetrain", default=None)
    crm_vin_parts.add_argument("--side", default=None)
    crm_vin_parts.add_argument("--axle", default=None)
    crm_vin_parts.add_argument("--position", default=None)
    crm_vin_parts.add_argument("--urgency", default=None)
    crm_vin_parts.add_argument("--city", default="Красноярск")
    crm_vin_parts.add_argument("--limit", type=int, default=10)
    crm_vin_parts.add_argument("--vin-oem-resolution-json", default=None)

    resolve_oem_parts = sub.add_parser(
        "resolve-vin-oem-parts",
        help="Resolve one VIN/frame and requested part into read-only OEM candidates, enrichment, gates, and manual actions",
    )
    resolve_oem_parts.add_argument("identifier")
    resolve_oem_parts.add_argument("--part", dest="requested_part", required=True)
    resolve_oem_parts.add_argument("--make", default=None)
    resolve_oem_parts.add_argument("--model", default=None)
    resolve_oem_parts.add_argument("--model-year", type=int, default=None)
    resolve_oem_parts.add_argument("--engine", default=None)
    resolve_oem_parts.add_argument("--transmission", default=None)
    resolve_oem_parts.add_argument("--market", default=None)
    resolve_oem_parts.add_argument("--drivetrain", default=None)
    resolve_oem_parts.add_argument("--axle", default=None)
    resolve_oem_parts.add_argument("--side", default=None)
    resolve_oem_parts.add_argument("--position", default=None)
    resolve_oem_parts.add_argument("--no-live-vpic", action="store_true")
    resolve_oem_parts.add_argument("--live-partsapi-identity", action="store_true")
    resolve_oem_parts.add_argument("--live-partsapi-oem", action="store_true")
    resolve_oem_parts.add_argument("--max-live-calls", type=int, default=3)
    resolve_oem_parts.add_argument("--max-candidates", type=int, default=3)
    resolve_oem_parts.add_argument("--timeout", type=float, default=20.0)
    resolve_oem_parts.add_argument("--max-attempts", type=int, default=1)
    resolve_oem_parts.add_argument("--partsapi-category-index", default=None)
    resolve_oem_parts.add_argument("--dry-run", action="store_true")

    vin_parts_benchmark = sub.add_parser(
        "vin-parts-benchmark",
        help="Benchmark a JSON batch of VIN/frame items for identity, part-intent, OEM/provider, and dry-run catalog readiness",
    )
    vin_parts_benchmark.add_argument("--items-json", required=True)
    vin_parts_benchmark.add_argument("--part", dest="requested_part", required=True)
    vin_parts_benchmark.add_argument("--city", default="Красноярск")
    vin_parts_benchmark.add_argument("--no-live-vpic", action="store_true")
    vin_parts_benchmark.add_argument("--no-vpic-batch", action="store_true")
    vin_parts_benchmark.add_argument("--skip-partsapi-dry-run", action="store_true")
    vin_parts_benchmark.add_argument("--skip-vin17-dry-run", action="store_true")
    vin_parts_benchmark.add_argument("--live-partsapi-identity", action="store_true")
    vin_parts_benchmark.add_argument("--live-partsapi-oem", action="store_true")
    vin_parts_benchmark.add_argument("--resolve-oem", action="store_true")
    vin_parts_benchmark.add_argument("--max-live-calls", type=int, default=3)
    vin_parts_benchmark.add_argument("--max-candidates", type=int, default=3)
    vin_parts_benchmark.add_argument("--partsapi-category-index", default=None)

    vin_parts_work_order = sub.add_parser(
        "vin-parts-work-order",
        help="Build per-card VIN/frame parts lookup work orders with OEM, cross, supplier, CRM writeback gates, and blockers",
    )
    vin_parts_work_order.add_argument("--items-json", required=True)
    vin_parts_work_order.add_argument("--part", dest="requested_part", required=True)
    vin_parts_work_order.add_argument("--city", default="Красноярск")
    vin_parts_work_order.add_argument("--no-live-vpic", action="store_true")
    vin_parts_work_order.add_argument("--no-vpic-batch", action="store_true")
    vin_parts_work_order.add_argument("--live-partsapi-identity", action="store_true")
    vin_parts_work_order.add_argument("--live-partsapi-oem", action="store_true")
    vin_parts_work_order.add_argument("--resolve-oem", action="store_true")
    vin_parts_work_order.add_argument("--max-live-calls", type=int, default=3)
    vin_parts_work_order.add_argument("--max-candidates", type=int, default=3)
    vin_parts_work_order.add_argument("--partsapi-category-index", default=None)

    source_route = sub.add_parser("source-route", help="Recommend authoritative automotive repair sources")
    source_route.add_argument("--brand", default=None)
    source_route.add_argument("--data-type", default=None)
    source_route.add_argument("--open-only", action="store_true")
    source_route.add_argument("--limit", type=int, default=10)

    maintenance_fluids = sub.add_parser(
        "maintenance-fluids",
        help="Recommend source routes for oils, fluids, capacities, and maintenance fill checks",
    )
    maintenance_fluids.add_argument("--brand", default=None)
    maintenance_fluids.add_argument("--unit", default=None)
    maintenance_fluids.add_argument("--vin", default=None)
    maintenance_fluids.add_argument("--chassis", default=None)
    maintenance_fluids.add_argument("--model", default=None)
    maintenance_fluids.add_argument("--year", type=int, default=None)
    maintenance_fluids.add_argument("--engine", dest="engine_code", default=None)
    maintenance_fluids.add_argument("--transmission", dest="transmission_code", default=None)
    maintenance_fluids.add_argument("--drivetrain", default=None)
    maintenance_fluids.add_argument("--market", default=None)
    maintenance_fluids.add_argument("--service-operation", default=None)
    maintenance_fluids.add_argument("--unit-variant", default=None)
    maintenance_fluids.add_argument("--fluid-spec", default=None)
    maintenance_fluids.add_argument("--level-check-procedure", default=None)
    maintenance_fluids.add_argument("--open-only", action="store_true")
    maintenance_fluids.add_argument("--limit", type=int, default=10)

    service_plan = sub.add_parser(
        "service-plan",
        help="Build a Krasnoyarsk workshop-management action plan for parts, repair, staff, client, finance, or knowledge work",
    )
    service_plan.add_argument("--area", default=None)
    service_plan.add_argument("--city", default="Красноярск")
    service_plan.add_argument("--vehicle", default=None)
    service_plan.add_argument("--vin", default=None)
    service_plan.add_argument("--chassis", default=None)
    service_plan.add_argument("--part-number", default=None)
    service_plan.add_argument("--part-name", default=None)
    service_plan.add_argument("--urgency", default=None)
    service_plan.add_argument("--role", default=None)
    service_plan.add_argument("--complaint", default=None)
    service_plan.add_argument("--dtc-or-scan", default=None)
    service_plan.add_argument("--engine", default=None)
    service_plan.add_argument("--transmission", default=None)
    service_plan.add_argument("--mileage", default=None)
    service_plan.add_argument("--current-load", default=None)
    service_plan.add_argument("--output-or-hours", default=None)
    service_plan.add_argument("--quality-signal", default=None)
    service_plan.add_argument("--card-id", default=None)
    service_plan.add_argument("--client-contact", default=None)
    service_plan.add_argument("--next-action", default=None)
    service_plan.add_argument("--approval-status", default=None)
    service_plan.add_argument("--repair-orders", default=None)
    service_plan.add_argument("--cashbox", default=None)
    service_plan.add_argument("--payment-status", default=None)
    service_plan.add_argument("--file-path", default=None)
    service_plan.add_argument("--source-type", default=None)
    service_plan.add_argument("--license-status", default=None)
    service_plan.add_argument("--target-playbook", default=None)
    service_plan.add_argument("--limit", type=int, default=10)

    estimate_work = sub.add_parser(
        "estimate-work",
        help="Build a read-only labor cost estimate from public Russia STO prices plus AutoStop 50%% markup",
    )
    estimate_work.add_argument("--vehicle", default=None)
    estimate_work.add_argument("--vin", default=None)
    estimate_work.add_argument("--chassis", default=None)
    estimate_work.add_argument("--make", default=None)
    estimate_work.add_argument("--model", default=None)
    estimate_work.add_argument("--year", type=int, default=None)
    estimate_work.add_argument("--engine", default=None)
    estimate_work.add_argument("--transmission", default=None)
    estimate_work.add_argument("--work", action="append", dest="work_items", default=[])
    estimate_work.add_argument("--complaint", default=None)
    estimate_work.add_argument("--city", default="Красноярск")
    estimate_work.add_argument("--quotes-json", default=None)
    estimate_work.add_argument("--auto-research", action=argparse.BooleanOptionalAction, default=True)
    estimate_work.add_argument("--labor-time-policy", choices=["public_only"], default="public_only")

    sub.add_parser("init", help="Initialize SQLite storage")
    sub.add_parser("seed-rules", help="Seed default manager rules from docs")

    sub.add_parser("knowledge-sync", help="Index the local knowledge map into SQLite")

    knowledge_intake = sub.add_parser(
        "knowledge-intake",
        help="Classify a source file and plan safe knowledge metadata updates",
    )
    knowledge_intake.add_argument("--path", required=True)
    knowledge_intake_mode = knowledge_intake.add_mutually_exclusive_group()
    knowledge_intake_mode.add_argument("--dry-run", action="store_true")
    knowledge_intake_mode.add_argument("--apply", action="store_true")

    knowledge_probe = sub.add_parser(
        "knowledge-probe",
        help="Quickly check whether local knowledge exists and return the first source-of-truth route",
    )
    knowledge_probe.add_argument("query")
    knowledge_probe.add_argument("--limit", type=int, default=5)

    knowledge_search = sub.add_parser("knowledge-search", help="Search indexed local knowledge routes and sections")
    knowledge_search.add_argument("query")
    knowledge_search.add_argument("--domain", default=None)
    knowledge_search.add_argument("--limit", type=int, default=10)

    sub.add_parser("knowledge-audit", help="Audit the local knowledge map, route cards, files, and SQLite index")

    sub.add_parser("cleanup-audit", help="Dry-run audit for cache, duplicate, and knowledge cleanup candidates")

    sub.add_parser("system-audit", help="Run the read-only AutoStop Manager health audit")
    sub.add_parser("doctor", help="Alias for system-audit")

    control_report = sub.add_parser(
        "control-report",
        help="Generate the Control Center V1 report as safe JSON or Markdown",
    )
    control_report.add_argument("--format", choices=["json", "markdown"], default="json")
    control_report.add_argument("--output", default=None)

    environment_report = sub.add_parser(
        "environment-report",
        help="Generate the deep server/Codex/Manager/CRM environment report as safe JSON or Markdown",
    )
    environment_report.add_argument("--format", choices=["json", "markdown"], default="json")
    environment_report.add_argument("--output", default=None)

    crm_health = sub.add_parser("crm-health-plan", help="Build a read-only CRM health plan from saved JSON payloads")
    crm_health.add_argument("--board-context-json", default=None)
    crm_health.add_argument("--board-review-json", default=None)
    crm_health.add_argument("--today-json", default=None)

    sub.add_parser("annotations-audit", help="Audit compact knowledge annotations used for fast routing")

    sub.add_parser("skills-audit", help="Audit local Codex skill files linked to knowledge routes")

    sub.add_parser("memory-audit", help="Audit long-term memory for duplicates, expired items, and superseded items")

    memory_curate = sub.add_parser("memory-curate", help="Curate long-term memory without deleting source records")
    memory_curate.add_argument("--apply", action="store_true")

    sub.add_parser("memory-review", help="Generate rule-based, non-destructive memory review proposals")

    memory_review_apply = sub.add_parser("memory-review-apply", help="Accept, reject, or archive duplicate memory review items")
    memory_review_apply.add_argument("--id", required=True)
    memory_review_apply.add_argument("--action", required=True, choices=["accept", "reject", "archive_duplicate"])

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    store = ManagerMemoryStore()

    if args.command == "init":
        store.initialize()
        seed_result = store.seed_default_rules()
        _print_json({"ok": True, "db_path": str(store.path), "seed_rules": seed_result})
    elif args.command == "seed-rules":
        _print_json(store.seed_default_rules())
    elif args.command == "knowledge-sync":
        _print_json(sync_knowledge_base(store))
    elif args.command == "knowledge-intake":
        _print_json(build_knowledge_intake_plan(args.path, apply=args.apply))
    elif args.command == "knowledge-probe":
        _print_json(probe_knowledge_base(store, args.query, limit=args.limit))
    elif args.command == "knowledge-search":
        _print_json(search_knowledge_base(store, args.query, domain=args.domain, limit=args.limit))
    elif args.command == "knowledge-audit":
        _print_json(audit_knowledge_base(store))
    elif args.command == "cleanup-audit":
        _print_json(build_cleanup_audit(store=store))
    elif args.command in {"system-audit", "doctor"}:
        _print_json(build_system_audit(store=store))
    elif args.command in {"control-report", "environment-report"}:
        report = build_control_report(store=store)
        if args.format == "markdown":
            rendered = format_control_report_markdown(report)
            _write_output(args.output, rendered)
            _print_text(rendered)
        else:
            rendered = json.dumps(report, ensure_ascii=False, indent=2)
            _write_output(args.output, rendered + "\n")
            _print_json(report)
    elif args.command == "crm-health-plan":
        _print_json(
            build_crm_health_plan(
                board_context=_json_file(args.board_context_json, option_name="--board-context-json"),
                board_review=_json_file(args.board_review_json, option_name="--board-review-json"),
                today_context=_json_file(args.today_json, option_name="--today-json"),
            )
        )
    elif args.command == "annotations-audit":
        _print_json(audit_knowledge_annotations(store))
    elif args.command == "skills-audit":
        _print_json(audit_skill_registry())
    elif args.command == "memory-audit":
        _print_json(audit_memory(store))
    elif args.command == "memory-curate":
        _print_json(curate_memory(store, apply=args.apply))
    elif args.command == "memory-review":
        _print_json(build_memory_review(store))
    elif args.command == "memory-review-apply":
        _print_json(apply_memory_review_item(args.id, args.action, store=store))
    elif args.command == "remember":
        _print_json(
            store.remember(
                args.content,
                kind=args.kind,
                title=args.title,
                category=args.category,
                source=args.source,
                tags=_tags(args.tags),
                importance=args.importance,
                confidence=args.confidence,
                expires_at=args.expires_at,
                supersedes_id=args.supersedes_id,
                sensitivity=args.sensitivity,
            )
        )
    elif args.command == "recall":
        _print_json(
            store.recall(
                args.query,
                limit=args.limit,
                kind=args.kind,
                category=args.category,
                tags=_tags(args.tags),
            )
        )
    elif args.command == "learn":
        _print_json(
            store.learn_from_feedback(
                args.content,
                title=args.title,
                applies_to=args.applies_to,
                signal=args.signal,
                recommendation=args.recommendation,
                avoid=args.avoid,
                importance=args.importance,
                confidence=args.confidence,
                source=args.source,
                tags=_tags(args.tags),
            )
        )
    elif args.command == "lessons":
        _print_json(
            store.recall_lessons(
                args.query,
                limit=args.limit,
                applies_to=args.applies_to,
                signal=args.signal,
                tags=_tags(args.tags),
            )
        )
    elif args.command == "memory-map":
        _print_json(store.memory_map())
    elif args.command == "memory-topics":
        _print_json(store.memory_topics(examples_limit=args.examples_limit))
    elif args.command == "memory-context":
        _print_json(store.memory_context_for(args.task, limit=args.limit))
    elif args.command == "memory-gaps":
        _print_json(store.memory_gaps())
    elif args.command == "task":
        _print_json(
            store.add_task(
                args.title,
                details=args.details,
                due_at=args.due,
                source=args.source,
                tags=_tags(args.tags),
            )
        )
    elif args.command == "remind":
        _print_json(
            store.add_reminder(
                args.title,
                remind_at=args.due,
                details=args.details,
                source=args.source,
                tags=_tags(args.tags),
            )
        )
    elif args.command == "journal":
        _print_json(store.journal(args.event, source=args.source, tags=_tags(args.tags)))
    elif args.command == "today":
        _print_json(store.today_context(limit=args.limit))
    elif args.command == "prepare-context":
        _print_json(prepare_manager_context(store, args.query, intent=args.intent, limit=args.limit))
    elif args.command == "agent-brief":
        _print_json(build_agent_brief(store, args.query, intent=args.intent, limit=args.limit))
    elif args.command == "prepare-card-action":
        _print_json(
            prepare_crm_card_action(
                card_id=args.card_id,
                expected_updated_at=args.expected_updated_at,
                description=args.description,
                vehicle_profile=_json_dict_arg(args.vehicle_profile_json, option_name="--vehicle-profile-json"),
                board_summary=args.board_summary,
                target_fields=_tags(args.target_fields),
                current_card=_json_dict_arg(args.current_card_json, option_name="--current-card-json"),
                intent=args.intent,
                dry_run=True,
            )
        )
    elif args.command == "lookup-oem":
        _print_json(
            lookup_original_parts(
                args.identifier,
                model_year=args.model_year,
                make_hint=args.make,
                part_name=args.part_name,
                part_group=args.part_group,
                side=args.side,
                position=args.position,
                old_part_number=args.old_part_number,
                captured_oem_number=args.captured_oem,
                captured_source=args.captured_source,
                captured_supersedes=args.captured_supersedes,
                captured_note=args.captured_note,
            )
        )
    elif args.command == "decode-vehicle":
        _print_json(
            decode_vehicle_identity(
                args.identifier,
                crm_context={
                    "vehicle": args.vehicle,
                    "make": args.make,
                    "model": args.model,
                    "model_year": args.model_year,
                    "engine": args.engine,
                    "transmission": args.transmission,
                    "drivetrain": args.drivetrain,
                    "market": args.market,
                    "source_confidence": args.source_confidence,
                },
                model_year=args.model_year,
                make_hint=args.make,
                live_vpic=not args.no_live_vpic,
            )
        )
    elif args.command == "decode-vehicles":
        items = _json_value(args.items_json, option_name="--items-json")
        if not isinstance(items, list):
            message = "--items-json must be a JSON array"
            raise SystemExit(message)
        _print_json(decode_vehicle_identities(items, live_vpic=not args.no_live_vpic, use_vpic_batch=not args.no_vpic_batch))
    elif args.command == "catalog-status":
        _print_json(catalog_provider_status(stage=args.stage))
    elif args.command == "provider-smoke":
        _print_json(build_provider_smoke_report(provider=args.provider, mode=args.mode))
    elif args.command == "oem-parts-provider-plan":
        identity = _json_dict_arg(args.vehicle_identity_json, option_name="--vehicle-identity-json")
        _print_json(
            build_oem_parts_provider_plan(
                identifier=args.identifier,
                requested_part=args.requested_part,
                vehicle_identity=identity,
                city=args.city,
            )
        )
    elif args.command == "vin17-decode":
        _print_json(vin17_decode_vehicle(args.identifier, dry_run=args.dry_run))
    elif args.command == "vin17-search-part":
        _print_json(
            vin17_search_part_number_by_vin(
                epc=args.epc,
                identifier=args.identifier,
                query_part_number=args.part_number,
                query_match_type=args.match_type,
                dry_run=args.dry_run,
            )
        )
    elif args.command == "partsapi-lookup":
        _print_json(
            partsapi_catalog_lookup(
                operation=args.operation,
                identifier=args.identifier,
                part_number=args.part_number,
                article_id=args.article_id,
                brand=args.brand,
                part_type=args.part_type,
                category=args.category,
                vehicle_type=args.vehicle_type,
                type_id=args.type_id,
                lang=args.lang,
                lang_id=args.lang_id,
                timeout=args.timeout,
                max_attempts=args.max_attempts,
                dry_run=args.dry_run,
            )
        )
    elif args.command == "partsapi-category-index":
        if args.category_index_command == "build":
            _print_json(
                build_partsapi_category_index_plan(
                    live=args.live,
                    vehicle_type=args.vehicle_type,
                    type_id=args.type_id,
                    lang_id=args.lang_id,
                    timeout=args.timeout,
                    max_attempts=args.max_attempts,
                )
            )
        elif args.category_index_command == "search":
            _print_json(search_partsapi_category_index(args.query, intent_id=args.intent_id, path=args.path, limit=args.limit))
        elif args.category_index_command == "explain":
            _print_json(explain_partsapi_category_for_intent(args.intent_id, query=args.query, path=args.path))
        elif args.category_index_command == "validate":
            _print_json(validate_partsapi_category_index(path=args.path))
    elif args.command == "public-catalog-lookup":
        _print_json(
            public_aftermarket_catalog_lookup(
                provider=args.provider,
                part_number=args.part_number,
                page_size=args.page_size,
                country=args.country,
                include_detail=not args.no_detail,
                dry_run=args.dry_run,
            )
        )
    elif args.command == "emex-price-lookup":
        _print_json(
            emex_price_lookup(
                part_number=args.part_number,
                brand=args.brand,
                subst_level=args.subst_level,
                subst_filter=args.subst_filter,
                delivery_region_type=args.delivery_region_type,
                min_delivery_percent=args.min_delivery_percent,
                max_delivery_days=args.max_delivery_days,
                min_quantity=args.min_quantity,
                max_result_price=args.max_result_price,
                max_one_detail_offers_count=args.max_one_detail_offers_count,
                detail_nums_to_load=_tags(args.detail_nums_to_load),
                timeout=args.timeout,
                dry_run=args.dry_run,
            )
        )
    elif args.command == "exist-price-lookup":
        _print_json(
            exist_price_lookup(
                part_number=args.part_number,
                brand=args.brand,
                pid=args.pid,
                office_id=args.office_id,
                max_candidates=args.max_candidates,
                max_offers=args.max_offers,
                include_more_offers=args.include_more_offers,
                timeout=args.timeout,
                dry_run=args.dry_run,
            )
        )
    elif args.command == "oem-catalog-lookup":
        _print_json(
            lookup_oem_catalog_candidates(
                identifier=args.identifier,
                requested_part=args.requested_part,
                catalog_id=args.catalog_id,
                car_id=args.car_id,
                group_id=args.group_id,
                epc=args.epc,
                partsapi_part_type=args.partsapi_part_type,
                partsapi_category=args.partsapi_category,
                timeout=args.timeout,
                max_attempts=args.max_attempts,
                dry_run=args.dry_run,
            )
        )
    elif args.command == "partsapi-vin-smoke":
        if args.repair_orders_json:
            raw_orders = _json_value(args.repair_orders_json, option_name="--repair-orders-json")
            if isinstance(raw_orders, dict):
                raw_orders = ((raw_orders.get("data") or {}).get("repair_orders") or raw_orders.get("repair_orders") or [])
            if not isinstance(raw_orders, list):
                message = "--repair-orders-json must be a JSON array or connector response object"
                raise SystemExit(message)
            selected = select_crm_partsapi_smoke_case(raw_orders, random_seed=args.random_seed, include_raw_identifier=True)
            if not selected.get("ok"):
                _print_json(selected)
                return 0
            selected_item = selected["selected"]
            item = {
                key: value
                for key, value in selected_item.items()
                if key not in {"identifier", "raw_identifier", "raw_identifier_is_sensitive"}
            }
            item["identifier"] = selected_item.get("raw_identifier")
        elif args.item_json:
            item = _json_dict_arg(args.item_json, option_name="--item-json")
            if not isinstance(item, dict):
                message = "--item-json must be a JSON object"
                raise SystemExit(message)
        else:
            item = {
                "identifier": args.identifier,
                "vehicle": args.vehicle,
                "requested_part": args.requested_part,
            }
        _print_json(
            build_partsapi_vin_smoke_report(
                item,
                requested_part=args.requested_part,
                partsapi_category=args.partsapi_category,
                part_type=args.part_type,
                max_candidates=args.max_candidates,
                timeout=args.timeout,
                live_vpic=not args.no_live_vpic,
                dry_run=args.dry_run,
            )
        )
    elif args.command == "crm-vin-parts-plan":
        vin_oem_resolution = _json_dict_arg(args.vin_oem_resolution_json, option_name="--vin-oem-resolution-json")
        _print_json(
            build_crm_vin_parts_lookup_pipeline(
                card_id=args.card_id,
                requested_part=args.requested_part,
                vin=args.vin,
                frame=args.frame,
                body_number=args.body_number,
                vehicle=args.vehicle,
                make=args.make,
                model=args.model,
                model_year=args.model_year,
                market=args.market,
                engine=args.engine,
                transmission=args.transmission,
                drivetrain=args.drivetrain,
                side=args.side,
                axle=args.axle,
                position=args.position,
                urgency=args.urgency,
                city=args.city,
                limit=args.limit,
                vin_oem_resolution=vin_oem_resolution,
            )
        )
    elif args.command == "resolve-vin-oem-parts":
        _print_json(
            resolve_vin_oem_parts(
                identifier=args.identifier,
                requested_part=args.requested_part,
                make=args.make,
                model=args.model,
                model_year=args.model_year,
                engine=args.engine,
                transmission=args.transmission,
                market=args.market,
                drivetrain=args.drivetrain,
                axle=args.axle,
                side=args.side,
                position=args.position,
                live_vpic=not args.no_live_vpic,
                live_partsapi_identity=args.live_partsapi_identity,
                live_partsapi_oem=args.live_partsapi_oem,
                max_live_calls=args.max_live_calls,
                max_candidates=args.max_candidates,
                timeout=args.timeout,
                max_attempts=args.max_attempts,
                partsapi_category_index=args.partsapi_category_index,
                dry_run=args.dry_run,
            )
        )
    elif args.command == "vin-parts-benchmark":
        items = _json_value(args.items_json, option_name="--items-json")
        if not isinstance(items, list):
            message = "--items-json must be a JSON array"
            raise SystemExit(message)
        _print_json(
            benchmark_vin_parts_lookup(
                items,
                requested_part=args.requested_part,
                city=args.city,
                live_vpic=not args.no_live_vpic,
                use_vpic_batch=not args.no_vpic_batch,
                include_partsapi_dry_run=not args.skip_partsapi_dry_run,
                include_vin17_dry_run=not args.skip_vin17_dry_run,
                live_partsapi_identity=args.live_partsapi_identity,
                live_partsapi_oem=args.live_partsapi_oem,
                resolve_oem=args.resolve_oem,
                max_live_calls=args.max_live_calls,
                max_candidates=args.max_candidates,
                partsapi_category_index=args.partsapi_category_index,
            )
        )
    elif args.command == "vin-parts-work-order":
        items = _json_value(args.items_json, option_name="--items-json")
        if not isinstance(items, list):
            message = "--items-json must be a JSON array"
            raise SystemExit(message)
        _print_json(
            build_vin_parts_work_order(
                items,
                requested_part=args.requested_part,
                city=args.city,
                live_vpic=not args.no_live_vpic,
                use_vpic_batch=not args.no_vpic_batch,
                live_partsapi_identity=args.live_partsapi_identity,
                live_partsapi_oem=args.live_partsapi_oem,
                resolve_oem=args.resolve_oem,
                max_live_calls=args.max_live_calls,
                max_candidates=args.max_candidates,
                partsapi_category_index=args.partsapi_category_index,
            )
        )
    elif args.command == "source-route":
        _print_json(
            recommend_automotive_sources(
                brand=args.brand,
                data_type=args.data_type,
                include_licensed=not args.open_only,
                limit=args.limit,
            )
        )
    elif args.command == "maintenance-fluids":
        _print_json(
            build_fluid_maintenance_plan(
                brand=args.brand,
                unit=args.unit,
                vin=args.vin,
                chassis=args.chassis,
                model=args.model,
                year=args.year,
                engine_code=args.engine_code,
                transmission_code=args.transmission_code,
                drivetrain=args.drivetrain,
                market=args.market,
                service_operation=args.service_operation,
                unit_variant=args.unit_variant,
                fluid_spec=args.fluid_spec,
                level_check_procedure=args.level_check_procedure,
                include_licensed=not args.open_only,
                limit=args.limit,
            )
        )
    elif args.command == "service-plan":
        _print_json(
            build_service_management_plan(
                area=args.area,
                city=args.city,
                vehicle=args.vehicle,
                vin=args.vin,
                chassis=args.chassis,
                part_number=args.part_number,
                part_name=args.part_name,
                urgency=args.urgency,
                role=args.role,
                complaint=args.complaint,
                dtc_or_scan=args.dtc_or_scan,
                engine=args.engine,
                transmission=args.transmission,
                mileage=args.mileage,
                current_load=args.current_load,
                output_or_hours=args.output_or_hours,
                quality_signal=args.quality_signal,
                card_id=args.card_id,
                client_contact=args.client_contact,
                next_action=args.next_action,
                approval_status=args.approval_status,
                repair_orders=args.repair_orders,
                cashbox=args.cashbox,
                payment_status=args.payment_status,
                file_path=args.file_path,
                source_type=args.source_type,
                license_status=args.license_status,
                target_playbook=args.target_playbook,
                limit=args.limit,
            )
        )
    elif args.command == "estimate-work":
        _print_json(
            estimate_repair_work_cost(
                vehicle=args.vehicle,
                vin=args.vin,
                chassis=args.chassis,
                make=args.make,
                model=args.model,
                year=args.year,
                engine=args.engine,
                transmission=args.transmission,
                work_items=args.work_items,
                complaint=args.complaint,
                city=args.city,
                quotes_json=_json_file(args.quotes_json, option_name="--quotes-json"),
                auto_research=args.auto_research,
                labor_time_policy=args.labor_time_policy,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
