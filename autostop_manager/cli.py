from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from .fluid_maintenance import build_fluid_maintenance_plan
from .service_management import build_service_management_plan
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

    recall = sub.add_parser("recall", help="Search manager memory")
    recall.add_argument("query", nargs="?", default="")
    recall.add_argument("--limit", type=int, default=20)

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
    elif args.command == "remember":
        _print_json(
            store.remember(
                args.content,
                kind=args.kind,
                title=args.title,
                category=args.category,
                source=args.source,
                tags=_tags(args.tags),
            )
        )
    elif args.command == "recall":
        _print_json(store.recall(args.query, limit=args.limit))
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
