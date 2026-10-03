#!/usr/bin/env python3
"""Install only a tested, committed maintenance payload; never release an app."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from disk_maintenance.install import install
from disk_maintenance.util import MaintenanceError


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--policy-file", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = install(args.source, args.policy_file, args.source_commit, args.receipt)
    except MaintenanceError as exc:
        print(json.dumps({"ok": False, "error": exc.code}))
        return 1
    print(
        json.dumps(
            {
                "ok": True,
                "status": result["status"],
                "revision": result["revision"],
                "payload_sha256": result["payload_sha256"],
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
