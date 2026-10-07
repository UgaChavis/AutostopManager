#!/usr/bin/env python3
"""Explicit read-only native benchmark; VIN is read from stdin, never stored."""

import argparse
import asyncio
from datetime import timedelta
import json
from pathlib import Path
import sys

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from autostop_manager.vin_retest import run_retest, save_receipt
from autostop_manager.mcp_contract import mcp_schema_fingerprint


async def main(args):
    identifier = sys.stdin.readline().strip().upper()
    async with httpx.AsyncClient(timeout=httpx.Timeout(args.timeout, connect=5), trust_env=False) as client:
        async with streamable_http_client(
            "http://127.0.0.1:41931/mcp", http_client=client, terminate_on_close=False
        ) as (reader, writer, _):
            async with ClientSession(reader, writer, read_timeout_seconds=timedelta(seconds=args.timeout)) as session:
                await session.initialize()
                schemas, cursor = {}, None
                for _ in range(20):
                    page = await session.list_tools(cursor=cursor)
                    schemas.update({tool.name: tool.inputSchema for tool in page.tools})
                    cursor = page.nextCursor
                    if not cursor:
                        break
                if cursor:
                    raise RuntimeError("Incomplete native tools/list")
                receipt = await run_retest(
                    session, identifier, schemas, timeout_seconds=args.timeout, full_control=args.full_control
                )
                receipt["native_tool_count"] = len(schemas)
                receipt["native_schema_fingerprint"] = mcp_schema_fingerprint(schemas)
                # Keep completed measurements even if transport cleanup stalls.
                save_receipt(args.output_dir, receipt)
                print(
                    json.dumps(
                        {
                            key: receipt[key]
                            for key in ("live_wall_ms", "manager_calls", "network_calls", "automatic_retries")
                        }
                    ),
                    flush=True,
                )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=25)
    parser.add_argument("--full-control", action="store_true")
    options = parser.parse_args()
    asyncio.run(asyncio.wait_for(main(options), timeout=2 * options.timeout + 10))
