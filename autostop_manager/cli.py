from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .catalog_clients import (
    emex_price_lookup,
    exist_price_lookup,
    lookup_oem_catalog_candidates,
)
from .cleanup_audit import build_cleanup_audit
from .control_center import build_control_report, format_control_report_markdown
from .context import build_agent_brief
from .crm_vin_parts import build_crm_vin_parts_lookup_pipeline
from .fluid_maintenance import build_fluid_maintenance_plan
from .gateway_attestation import (
    DEFAULT_MCP_URL as DEFAULT_GATEWAY_ATTESTATION_MCP_URL,
    DEFAULT_OUTPUT_ROOT as DEFAULT_GATEWAY_ATTESTATION_OUTPUT_ROOT,
    default_gateway_attestation_run_id,
    run_gateway_attestation,
)
from .integration_audit import build_integration_audit
from .knowledge_base import (
    audit_knowledge_base,
    probe_knowledge_base,
    search_knowledge_base,
    sync_knowledge_base,
)
from .knowledge_intake import build_knowledge_intake_plan
from .memory_review import build_memory_review
from .partsapi_smoke import build_partsapi_vin_smoke_report, select_crm_partsapi_smoke_case
from .provider_smoke import build_provider_smoke_report
from .service_labor_experience import (
    build_service_labor_experience_from_state_file,
    save_service_labor_artifacts,
    summarize_service_labor_snapshot,
)
from .service_labor_report import build_service_labor_report_artifact, save_service_labor_report_artifact
from .skill_registry import audit_skill_registry
from .storage import ManagerMemoryStore
from .system_audit import build_system_audit
from .vehicle_identity import decode_vehicle_identities, decode_vehicle_identity
from .vin_parts_benchmark import benchmark_vin_parts_lookup
from .vin_parts_work_order import build_vin_parts_work_order
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


def _print_checked_json(payload: dict[str, Any]) -> int:
    _print_json(payload)
    return 0 if payload.get("ok") is True else 1


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
    recall.add_argument(
        "--kind", choices=["note", "fact", "lesson", "task", "reminder", "journal", "rule"], default=None
    )
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

    agent_mode = sub.add_parser("agent-mode", help="Read or change the durable AgentExecutionMode")
    agent_mode_sub = agent_mode.add_subparsers(dest="agent_mode_action", required=True)
    agent_mode_sub.add_parser("status", help="Show global work/learning mode")
    agent_mode_set = agent_mode_sub.add_parser("set", help="Set global work/learning mode")
    agent_mode_set.add_argument("mode", choices=["work", "learning"])
    agent_mode_set.add_argument("--expected-state-version", type=int, default=None)
    agent_mode_resolve = agent_mode_sub.add_parser("resolve", help="Resolve a one-turn mode override")
    agent_mode_resolve.add_argument("--mode-override", choices=["work", "learning"], default=None)

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

    director_journal = sub.add_parser(
        "director-journal",
        help="Create, read, update, inspect, or prune the bounded de-identified director journal",
    )
    director_journal.add_argument(
        "operation",
        choices=["create", "read", "update", "stats", "prune"],
    )
    director_journal.add_argument("--event", default="")
    director_journal.add_argument("--entry-id", type=int, default=None)
    director_journal.add_argument("--category", default="")
    director_journal.add_argument("--decision", default="")
    director_journal.add_argument("--result", default="")
    director_journal.add_argument("--status", default="")
    director_journal.add_argument("--next-review-at", default=None)
    director_journal.add_argument("--expected-updated-at", default="")
    director_journal.add_argument("--workflow-ref-hash", default="")
    director_journal.add_argument("--source", default="director")
    director_journal.add_argument("--tags", default="")
    director_journal.add_argument("--limit", type=int, default=10)
    director_journal.add_argument("--apply", action="store_true")

    today = sub.add_parser("today", help="Return today's manager context")
    today.add_argument("--limit", type=int, default=20)

    agent_brief = sub.add_parser(
        "agent-brief",
        help="Return a compact startup package for an agent before broad document reads",
    )
    agent_brief.add_argument("query")
    agent_brief.add_argument("--intent", default=None)
    agent_brief.add_argument("--limit", type=int, default=8)

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

    provider_smoke = sub.add_parser(
        "provider-smoke",
        help="Run safe provider readiness smoke checks without supplier orders, baskets, or CRM writeback",
    )
    provider_smoke.add_argument("--provider", default="all")
    provider_smoke.add_argument("--mode", choices=["dry-run", "live-readonly"], default="dry-run")

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

    estimate_work = sub.add_parser(
        "estimate-work",
        help="Build a read-only multi-source labor estimate from internal experience, market, and labor time",
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
    estimate_work.add_argument(
        "--internal-experience",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Use the aggregate-only local closed-order experience snapshot",
    )

    labor_refresh = sub.add_parser(
        "service-labor-refresh",
        help="Refresh labor-only aggregate experience from all closed CRM repair orders",
    )
    labor_refresh.add_argument("--state-json", required=True)
    labor_refresh.add_argument("--half-life-days", type=int, default=90)
    labor_refresh.add_argument(
        "--output",
        default="data/private_knowledge/service_labor_experience.json",
    )
    labor_refresh.add_argument(
        "--executor-output",
        default="data/private_knowledge/restricted/service_labor_executor_report.json",
    )
    labor_refresh.add_argument(
        "--report-output",
        default="data/private_knowledge/reports/service_labor_analysis.md",
    )
    labor_refresh.add_argument(
        "--artifact-output",
        default="data/private_knowledge/reports/service_labor_analysis.artifact.json",
    )

    sub.add_parser("init", help="Initialize SQLite storage")

    store_checkpoint_status = sub.add_parser(
        "store-checkpoint-status",
        help="Show one scoped Store digest/bootstrap delivery checkpoint",
    )
    store_checkpoint_status.add_argument(
        "--stream",
        required=True,
        choices=["store_digest", "store_bootstrap"],
    )

    store_checkpoint_reset = sub.add_parser(
        "store-checkpoint-reset",
        help="Reset one verified-broken Store cursor so its next read creates a fresh baseline",
    )
    store_checkpoint_reset.add_argument(
        "--stream",
        required=True,
        choices=["store_digest", "store_bootstrap"],
    )
    store_checkpoint_reset.add_argument("--expected-state-version", required=True, type=int)
    store_checkpoint_reset.add_argument(
        "--reason",
        required=True,
        choices=[
            "cursor_generation_mismatch",
            "cursor_ahead_after_store_restore",
            "operator_verified_rebaseline",
        ],
    )
    store_checkpoint_reset.add_argument(
        "--confirm-rebaseline",
        required=True,
        action="store_true",
        help="Acknowledge that only this Store stream will discard its cursor and baseline again",
    )

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

    sub.add_parser("knowledge-audit", help="Audit the live knowledge map, files, and document index")

    sub.add_parser("cleanup-audit", help="Dry-run audit for cache, duplicate, and knowledge cleanup candidates")

    sub.add_parser("doctor", help="Run the read-only AutoStop Manager health audit")

    integration_audit = sub.add_parser(
        "integration-audit",
        help="Verify live CRM, Store, web research, Gmail readiness, and docs/runtime contracts",
    )
    integration_audit.add_argument("--full", action="store_true")
    integration_audit.add_argument(
        "--gmail-proof",
        default="/var/lib/autostop-manager/integration/gmail-proof.json",
    )
    integration_audit.add_argument("--output", default=None)

    gateway_attestation = sub.add_parser(
        "crm-gateway-attest",
        help="Run one stop-the-line AutoStop CRM Gateway v2 attestation step",
    )
    gateway_attestation.add_argument(
        "action",
        choices=["inventory", "next", "resume", "case", "retry", "cleanup", "summary"],
    )
    gateway_attestation.add_argument("--run-id", default="")
    gateway_attestation.add_argument("--case-id", default="")
    gateway_attestation.add_argument("--apply-synthetic", action="store_true")
    gateway_attestation.add_argument("--force", action="store_true")
    gateway_attestation.add_argument(
        "--mcp-url",
        default=DEFAULT_GATEWAY_ATTESTATION_MCP_URL,
    )
    gateway_attestation.add_argument(
        "--output-root",
        default=str(DEFAULT_GATEWAY_ATTESTATION_OUTPUT_ROOT),
    )

    control_report = sub.add_parser(
        "control-report",
        help="Generate the Control Center V1 report as safe JSON or Markdown",
    )
    control_report.add_argument("--format", choices=["json", "markdown"], default="json")
    control_report.add_argument("--output", default=None)

    sub.add_parser("skills-audit", help="Audit local Codex skill files linked to knowledge routes")

    sub.add_parser("memory-review", help="Generate rule-based, non-destructive memory review proposals")

    return parser


# This intentionally flat dispatcher mirrors argparse commands; domain logic lives in focused modules above.
def main(argv: list[str] | None = None) -> int:  # noqa: C901
    args = build_parser().parse_args(argv)
    store = ManagerMemoryStore()

    if args.command == "init":
        store.initialize()
        _print_json({"ok": True, "db_path": str(store.path)})
    elif args.command == "store-checkpoint-status":
        return _print_checked_json(store.get_store_checkpoint(args.stream))
    elif args.command == "store-checkpoint-reset":
        return _print_checked_json(
            store.reset_store_checkpoint_for_rebaseline(
                stream=args.stream,
                expected_state_version=args.expected_state_version,
                reason=args.reason,
            )
        )
    elif args.command == "knowledge-sync":
        return _print_checked_json(sync_knowledge_base(store))
    elif args.command == "knowledge-intake":
        _print_json(build_knowledge_intake_plan(args.path, apply=args.apply))
    elif args.command == "knowledge-probe":
        _print_json(probe_knowledge_base(store, args.query, limit=args.limit))
    elif args.command == "knowledge-search":
        _print_json(search_knowledge_base(store, args.query, domain=args.domain, limit=args.limit))
    elif args.command == "knowledge-audit":
        return _print_checked_json(audit_knowledge_base(store))
    elif args.command == "cleanup-audit":
        return _print_checked_json(build_cleanup_audit(store=store))
    elif args.command == "doctor":
        return _print_checked_json(build_system_audit(store=store))
    elif args.command == "integration-audit":
        report = build_integration_audit(full=args.full, gmail_proof_path=args.gmail_proof)
        _write_output(args.output, json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        return _print_checked_json(report)
    elif args.command == "crm-gateway-attest":
        run_id = args.run_id or (default_gateway_attestation_run_id() if args.action == "inventory" else "")
        if not run_id:
            raise SystemExit("--run-id is required after inventory")
        if args.force and args.action not in {"inventory", "case"}:
            raise SystemExit("--force is valid only for inventory or case")
        if args.apply_synthetic and args.action in {
            "inventory",
            "cleanup",
            "summary",
        }:
            raise SystemExit("--apply-synthetic is valid only for next, resume, case, or retry")
        return _print_checked_json(
            run_gateway_attestation(
                action=args.action,
                run_id=run_id,
                mcp_url=args.mcp_url,
                output_root=args.output_root,
                case_id=args.case_id,
                apply_synthetic=args.apply_synthetic,
                force=args.force,
            )
        )
    elif args.command == "control-report":
        report = build_control_report(store=store)
        if args.format == "markdown":
            rendered = format_control_report_markdown(report)
            _write_output(args.output, rendered)
            _print_text(rendered)
        else:
            rendered = json.dumps(report, ensure_ascii=False, indent=2)
            _write_output(args.output, rendered + "\n")
            _print_json(report)
    elif args.command == "skills-audit":
        return _print_checked_json(audit_skill_registry())
    elif args.command == "memory-review":
        _print_json(build_memory_review(store))
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
    elif args.command == "agent-mode":
        if args.agent_mode_action == "status":
            _print_json(store.get_agent_mode())
        elif args.agent_mode_action == "set":
            _print_json(store.set_agent_mode(args.mode, expected_state_version=args.expected_state_version))
        else:
            _print_json(store.resolve_agent_mode(args.mode_override))
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
    elif args.command == "director-journal":
        if args.operation == "create":
            return _print_checked_json(
                store.create_director_journal_entry(
                    args.event,
                    category=args.category or "operations",
                    decision=args.decision,
                    result=args.result,
                    status=args.status or "open",
                    next_review_at=args.next_review_at,
                    workflow_ref_hash=args.workflow_ref_hash,
                    source=args.source,
                    tags=_tags(args.tags),
                )
            )
        if args.operation == "read":
            return _print_checked_json(
                store.read_director_journal(
                    status=args.status or "active",
                    category=args.category,
                    limit=args.limit,
                )
            )
        if args.operation == "update":
            if args.entry_id is None:
                return _print_checked_json({"ok": False, "error": "entry_id_required"})
            return _print_checked_json(
                store.update_director_journal_entry(
                    args.entry_id,
                    status=args.status,
                    result=args.result,
                    next_review_at=args.next_review_at,
                    expected_updated_at=args.expected_updated_at,
                )
            )
        if args.operation == "stats":
            return _print_checked_json(store.director_journal_stats())
        return _print_checked_json(store.maintain_director_journal(apply=args.apply))
    elif args.command == "today":
        _print_json(store.today_context(limit=args.limit))
    elif args.command == "agent-brief":
        _print_json(build_agent_brief(store, args.query, intent=args.intent, limit=args.limit))
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
        _print_json(
            decode_vehicle_identities(items, live_vpic=not args.no_live_vpic, use_vpic_batch=not args.no_vpic_batch)
        )
    elif args.command == "provider-smoke":
        _print_json(build_provider_smoke_report(provider=args.provider, mode=args.mode))
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
        item: dict[str, Any]
        if args.repair_orders_json:
            raw_orders = _json_value(args.repair_orders_json, option_name="--repair-orders-json")
            if isinstance(raw_orders, dict):
                raw_orders = (
                    (raw_orders.get("data") or {}).get("repair_orders") or raw_orders.get("repair_orders") or []
                )
            if not isinstance(raw_orders, list):
                message = "--repair-orders-json must be a JSON array or connector response object"
                raise SystemExit(message)
            selected = select_crm_partsapi_smoke_case(
                raw_orders, random_seed=args.random_seed, include_raw_identifier=True
            )
            if not selected.get("ok"):
                return _print_checked_json(selected)
            selected_item = selected["selected"]
            item = {
                key: value
                for key, value in selected_item.items()
                if key not in {"identifier", "raw_identifier", "raw_identifier_is_sensitive"}
            }
            item["identifier"] = selected_item.get("raw_identifier")
        elif args.item_json:
            parsed_item = _json_dict_arg(args.item_json, option_name="--item-json")
            if not isinstance(parsed_item, dict):
                message = "--item-json must be a JSON object"
                raise SystemExit(message)
            item = parsed_item
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
                use_internal_experience=args.internal_experience,
            )
        )
    elif args.command == "service-labor-refresh":
        snapshot, executor_report = build_service_labor_experience_from_state_file(
            args.state_json,
            recency_half_life_days=args.half_life_days,
        )
        paths = save_service_labor_artifacts(
            snapshot,
            executor_report,
            output_path=args.output,
            executor_output_path=args.executor_output,
            report_output_path=args.report_output,
        )
        artifact_output_path = save_service_labor_report_artifact(
            build_service_labor_report_artifact(snapshot),
            args.artifact_output,
        )
        _print_json(
            {
                "ok": True,
                **paths,
                "artifact_output_path": str(artifact_output_path),
                **summarize_service_labor_snapshot(snapshot),
                "executor_report": {
                    "restricted": True,
                    "executor_count": len(executor_report.get("executors") or []),
                    "must_not_feed_customer_pricing": True,
                },
            }
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
