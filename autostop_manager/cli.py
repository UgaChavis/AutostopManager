from __future__ import annotations

import argparse
import json
from typing import Any

from .storage import ManagerMemoryStore


def _tags(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


def _print_json(payload: dict[str, Any]) -> None:
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
