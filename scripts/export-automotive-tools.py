#!/usr/bin/env python3
"""Export from a Git snapshot, never from an uncommitted checkout."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--revision", required=True, help="Exact published Manager commit SHA")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    revision = subprocess.check_output(
        ["git", "rev-parse", "--verify", f"{args.revision}^{{commit}}"], cwd=root, text=True
    ).strip()
    if revision != args.revision:
        parser.error("--revision must be an exact 40-character commit SHA")
    with tempfile.TemporaryDirectory(prefix="autostop-catalog-") as temp:
        snapshot = Path(temp) / "source"
        snapshot.mkdir()
        archive = subprocess.Popen(["git", "archive", revision], cwd=root, stdout=subprocess.PIPE)
        extracted = subprocess.run(["tar", "-xf", "-", "-C", str(snapshot)], stdin=archive.stdout, check=False)
        if archive.stdout:
            archive.stdout.close()
        if archive.wait() or extracted.returncode:
            raise RuntimeError("snapshot_export_failed")
        code = """
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from mcp.server.fastmcp import FastMCP
from autostop_manager.mcp_tools import register_manager_tools
from autostop_manager.catalog_clients import PARTSAPI_OPERATIONS
from autostop_manager.automotive_catalog import build_bundle
root = Path(sys.argv[1]); server = FastMCP('catalog-export')
register_manager_tools(server)
schemas = {name: tool.parameters for name,tool in server._tool_manager._tools.items()}
registry = json.loads((root/'docs/agent/automotive_tools.json').read_text())
print(json.dumps(build_bundle(root,registry,schemas,PARTSAPI_OPERATIONS,sys.argv[2]),ensure_ascii=False,sort_keys=True,indent=2))
"""
        environment = {
            **os.environ,
            "AUTOSTOP_MANAGER_ENV_FILE": os.devnull,
            "AUTOSTOP_MANAGER_DB": str(Path(temp) / "schema.sqlite3"),
            "PYTHONSAFEPATH": "1",
        }
        data = subprocess.check_output(
            [sys.executable, "-I", "-c", code, str(snapshot), revision], cwd=temp, env=environment
        )
        payload = json.loads(data)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_bytes(data)
        print(
            json.dumps(
                {
                    "ok": True,
                    "source_revision": revision,
                    "content_hash": payload["content_hash"],
                    "output": str(args.output),
                }
            )
        )


if __name__ == "__main__":
    main()
