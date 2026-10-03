#!/usr/bin/env python3
"""Isolated entry point for the root-owned maintenance package."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Python -I deliberately omits the script directory. This installed directory is
# a root-owned, immutable release; no application or caller cwd is imported.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from disk_maintenance import core
from disk_maintenance.util import MaintenanceError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", type=Path, default=core.DEFAULT_POLICY_PATH)
    commands = parser.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan")
    plan.add_argument("--output", type=Path, required=True)
    apply = commands.add_parser("apply")
    apply.add_argument("--manifest", type=Path, required=True)
    apply.add_argument("--approve", required=True)
    for name in ("maintain", "pg-retain", "recover-ci"):
        commands.add_parser(name)
    arguments = parser.parse_args(argv)
    try:
        policy = core.load_policy(arguments.policy)
        if arguments.command == "plan":
            result = core.save_plan(policy, arguments.output)
        elif arguments.command == "apply":
            result = core.apply(policy, arguments.manifest, arguments.approve)
        else:
            function = {"maintain": core.maintain, "pg-retain": core.pg_retain, "recover-ci": core.recover_ci}[
                arguments.command
            ]
            result = function(policy)
        # stdout is technical metadata only, never paths/config/command stderr.
        summary = {
            key: result[key]
            for key in (
                "ok",
                "manifest_sha256",
                "candidate_count",
                "free_before",
                "free_after",
                "goal_bytes",
                "deficit_bytes",
                "target_reached",
                "ci_restored",
                "ci_warnings",
                "retention_pending",
                "errors",
            )
            if key in result
        }
        print(json.dumps(summary, sort_keys=True))
        return 0 if result.get("ok") is True else 2
    except (MaintenanceError, OSError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": core._error_code(exc)}, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
