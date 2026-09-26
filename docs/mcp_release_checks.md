# Native MCP activation and Codex refresh

A coordinated CRM deploy with `AUTOSTOP_MANAGER_MCP_ACTIVATE_ON_DEPLOY=1`
installs, restarts and probes native MCP inside the rollback window. Do not run a
second component installer after a successful deploy. Require
`autostop-manager-mcp.service` active, verify that
`/proc/$(systemctl show -p MainPID --value autostop-manager-mcp.service)/cwd`
resolves to the same directory as `/opt/autostop-manager-releases/current`, then
run the active revision through `/opt/AutostopManager/.venv/bin/python -m
autostop_manager.cli mcp-probe
--url http://127.0.0.1:41931/mcp --provider-failure-check --store-check` with
`PYTHONPATH=/opt/autostop-manager-releases/current`. Use
`scripts/install-manager-mcp.sh --activate` only for an explicitly scoped
standalone recovery with preserved rollback state. Never print key values when
checking process/configuration parity.

`--store-check` checks health/capabilities and reads at most one order through
`store_search(entity="store_order", limit=1)`. It never mutates Store or follows
pagination. Require `checks.store_order_search.ok=true`; schema drift fails the
probe even when health is green. The report retains only `ok` and a fixed
diagnostic, never order IDs, fields, or raw provider errors. An empty Store is
reported as `store_order_sample_empty`: access works, but no order was available
to exercise its field contract. Run this check after the Store cutover settles.

After the J1 browser probe reports both `browser_ready=true` and
`browser_containers_ready=true`, rerun the native probe with
`--timeout 90 --browser-check`. Require `checks.fetch_page_browser.ok=true`; this calls the
public synthetic page `https://example.com/` through the Manager MCP tool and
does not retain its page text in the report. A green browser-stack probe alone
does not prove that the advertised Manager tool is wired to it.

Reconnect the existing Codex client using the supported App Server JSON-RPC
`config/mcpServer/reload` request (`params: null`). It queues a refresh for loaded
tasks; the current model turn may retain its original tool declarations until
the next turn. Verify `mcpServerStatus/list` for the task and the endpoint's
`tools/list`: `partsapi_catalog_lookup` must include `provider_parameters` and
`supplier_id`. A reload acknowledgement alone is not a successful tool call.
See the [App Server protocol](https://developers.openai.com/codex/app-server).

Verify initialize/ping, safe tool calls, invalid-input rejection and continued
session usability. Check `norms_models` with a lowercase make code in dry-run
mode, then a bounded live engine/catalog lookup when authorized. Do not confuse
provider authentication or quota errors with an MCP transport outage. Restore
the previous Telegram work-mode state after release checks succeed.
