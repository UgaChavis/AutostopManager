from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .cleanup_audit import build_cleanup_audit
from .control_center import build_control_report, format_control_report_markdown
from .context import build_agent_brief
from .integration_audit import build_integration_audit
from .knowledge_base import (
    audit_knowledge_base,
    probe_knowledge_base,
    sync_knowledge_base,
)
from .memory_curator import audit_memory
from .skill_registry import audit_skill_registry
from .storage import ManagerMemoryStore
from .system_audit import build_system_audit


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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="autostop-manager", description="AutoStop manager memory CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    agent_brief = sub.add_parser(
        "agent-brief",
        help="Return a compact startup package for an agent before broad document reads",
    )
    agent_brief.add_argument("query")
    agent_brief.add_argument("--intent", default=None)
    agent_brief.add_argument("--limit", type=int, default=8)

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

    knowledge_probe = sub.add_parser(
        "knowledge-probe",
        help="Quickly check whether local knowledge exists and return the first source-of-truth route",
    )
    knowledge_probe.add_argument("query")
    knowledge_probe.add_argument("--limit", type=int, default=5)

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
def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    store = ManagerMemoryStore()

    if args.command == "store-checkpoint-status":
        return _print_checked_json(store.get_store_checkpoint(args.stream))
    if args.command == "store-checkpoint-reset":
        return _print_checked_json(
            store.reset_store_checkpoint_for_rebaseline(
                stream=args.stream,
                expected_state_version=args.expected_state_version,
                reason=args.reason,
            )
        )
    if args.command == "knowledge-sync":
        return _print_checked_json(sync_knowledge_base(store))
    if args.command == "knowledge-probe":
        _print_json(probe_knowledge_base(store, args.query, limit=args.limit))
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
        _print_json(audit_memory(store))
    elif args.command == "agent-brief":
        _print_json(build_agent_brief(store, args.query, intent=args.intent, limit=args.limit))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
