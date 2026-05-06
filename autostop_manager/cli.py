from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from .context import prepare_manager_context
from .fluid_maintenance import build_fluid_maintenance_plan
from .knowledge_base import (
    audit_knowledge_annotations,
    audit_knowledge_base,
    probe_knowledge_base,
    search_knowledge_base,
    sync_knowledge_base,
)
from .memory_curator import audit_memory, curate_memory
from .service_management import build_service_management_plan
from .skill_registry import audit_skill_registry
from .source_catalog import recommend_automotive_sources
from .storage import ManagerMemoryStore
from .vin_lookup import lookup_original_parts


def _tags(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


def _print_json(payload: dict[str, Any]) -> None:
    stdout_reconfigure = getattr(sys.stdout, "reconfigure", None)
    if stdout_reconfigure is not None:
        stdout_reconfigure(encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _json_object(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {"raw": raw}
    return value if isinstance(value, dict) else {"value": value}


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

    lookup = sub.add_parser("lookup-oem", help="Classify a VIN or frame number and return OEM lookup routing")
    lookup.add_argument("identifier")
    lookup.add_argument("--model-year", type=int, default=None)
    lookup.add_argument("--make", default=None)

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

    sub.add_parser("init", help="Initialize SQLite storage")
    sub.add_parser("seed-rules", help="Seed default manager rules from docs")

    sub.add_parser("knowledge-sync", help="Index the local knowledge map into SQLite")

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

    sub.add_parser("annotations-audit", help="Audit compact knowledge annotations used for fast routing")

    sub.add_parser("skills-audit", help="Audit local Codex skill files linked to knowledge routes")

    sub.add_parser("memory-audit", help="Audit long-term memory for duplicates, expired items, and superseded items")

    memory_curate = sub.add_parser("memory-curate", help="Curate long-term memory without deleting source records")
    memory_curate.add_argument("--apply", action="store_true")

    run_start = sub.add_parser("run-start", help="Start an auditable manager operation run")
    run_start.add_argument("query")
    run_start.add_argument("--intent", default="")
    run_start.add_argument("--dry-run", action="store_true", dest="dry_run")
    run_start.add_argument("--source", default="codex")
    run_start.add_argument("--metadata", default="")

    run_event = sub.add_parser("run-event", help="Record a manager run event")
    run_event.add_argument("run_id", type=int)
    run_event.add_argument("--type", dest="event_type", required=True)
    run_event.add_argument("--message", default="")
    run_event.add_argument("--target-type", default="")
    run_event.add_argument("--target-id", default="")
    run_event.add_argument("--payload", default="")

    run_finish = sub.add_parser("run-finish", help="Finish a manager operation run")
    run_finish.add_argument("run_id", type=int)
    run_finish.add_argument("--status", default="completed")
    run_finish.add_argument("--summary", default="")
    run_finish.add_argument("--verification", default="")

    run_list = sub.add_parser("run-list", help="List manager operation runs")
    run_list.add_argument("--limit", type=int, default=20)
    run_list.add_argument("--events", action="store_true")
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
    elif args.command == "knowledge-probe":
        _print_json(probe_knowledge_base(store, args.query, limit=args.limit))
    elif args.command == "knowledge-search":
        _print_json(search_knowledge_base(store, args.query, domain=args.domain, limit=args.limit))
    elif args.command == "knowledge-audit":
        _print_json(audit_knowledge_base(store))
    elif args.command == "annotations-audit":
        _print_json(audit_knowledge_annotations(store))
    elif args.command == "skills-audit":
        _print_json(audit_skill_registry())
    elif args.command == "memory-audit":
        _print_json(audit_memory(store))
    elif args.command == "memory-curate":
        _print_json(curate_memory(store, apply=args.apply))
    elif args.command == "run-start":
        _print_json(
            store.start_manager_run(
                intent=args.intent,
                query=args.query,
                dry_run=args.dry_run,
                source=args.source,
                metadata=_json_object(args.metadata),
            )
        )
    elif args.command == "run-event":
        _print_json(
            store.record_manager_run_event(
                args.run_id,
                event_type=args.event_type,
                message=args.message,
                target_type=args.target_type,
                target_id=args.target_id,
                payload=_json_object(args.payload),
            )
        )
    elif args.command == "run-finish":
        _print_json(
            store.finish_manager_run(
                args.run_id,
                status=args.status,
                summary=args.summary,
                verification=_json_object(args.verification),
            )
        )
    elif args.command == "run-list":
        _print_json(store.list_manager_runs(limit=args.limit, include_events=args.events))
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
    elif args.command == "lookup-oem":
        _print_json(lookup_original_parts(args.identifier, model_year=args.model_year, make_hint=args.make))
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
