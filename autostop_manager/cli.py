from __future__ import annotations

import argparse
import json
from typing import Any

from .diagnostics import audit_documentation, diagnose
from .mcp_probe import DEFAULT_MANAGER_MCP_URL, probe_manager_mcp
from .storage import StoreState


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AutoStop Manager operations")
    sub = parser.add_subparsers(dest="command", required=True)
    # External CRM deployment still invokes these names; neither writes a database.
    for command in ("knowledge-sync", "knowledge-audit"):
        sub.add_parser(command, help="Validate local documentation (no database writes)")
    doctor = sub.add_parser("doctor", help="Local checks; live integrations require an explicit flag")
    doctor.add_argument("--integrations", action="store_true")
    doctor.add_argument("--full", action="store_true")
    sub.add_parser("store-conductor-release-gate", help="Read-only legacy Store-state release gate")
    status = sub.add_parser("store-checkpoint-status")
    status.add_argument("--stream", required=True, choices=["store_digest", "store_bootstrap"])
    reset = sub.add_parser("store-checkpoint-reset")
    reset.add_argument("--stream", required=True, choices=["store_digest", "store_bootstrap"])
    reset.add_argument("--expected-state-version", required=True, type=int)
    reset.add_argument(
        "--reason",
        required=True,
        choices=[
            "cursor_generation_mismatch",
            "cursor_ahead_after_store_restore",
            "operator_verified_rebaseline",
        ],
    )
    reset.add_argument("--confirm-rebaseline", required=True, action="store_true")
    probe = sub.add_parser("mcp-probe", help="Synthetic read-only native MCP transport check")
    probe.add_argument("--url", default=DEFAULT_MANAGER_MCP_URL)
    probe.add_argument("--timeout", type=float, default=10)
    probe.add_argument("--provider-failure-check", action="store_true")
    probe.add_argument("--store-check", action="store_true", help="Also verify the direct Manager-to-Store connection")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result: dict[str, Any]
    if args.command in {"knowledge-sync", "knowledge-audit"}:
        result = audit_documentation()
    elif args.command == "doctor":
        result = diagnose(integrations=args.integrations, full=args.full)
    elif args.command == "mcp-probe":
        result = probe_manager_mcp(
            args.url,
            timeout=args.timeout,
            provider_failure_check=args.provider_failure_check,
            store_check=args.store_check,
        )
    else:
        store = StoreState()
        if args.command == "store-conductor-release-gate":
            result = store.store_quote_conductor_release_readiness()
        elif args.command == "store-checkpoint-status":
            result = store.get_store_checkpoint(args.stream)
        else:
            result = store.reset_store_checkpoint_for_rebaseline(
                stream=args.stream,
                expected_state_version=args.expected_state_version,
                reason=args.reason,
            )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
