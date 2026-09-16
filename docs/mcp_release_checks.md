# Native MCP activation and Codex refresh

A successful CRM deploy switches the shared Manager snapshot, but does not by
itself prove that the native MCP process has restarted. After deploy, run the
active snapshot's `scripts/install-manager-mcp.sh --activate` and verify that
`/proc/$(systemctl show -p MainPID --value autostop-manager-mcp.service)/cwd`
resolves to the same directory as `/opt/autostop-manager-releases/current`.
Environment changes also require this restart; never print key values when
checking process/configuration parity.

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

