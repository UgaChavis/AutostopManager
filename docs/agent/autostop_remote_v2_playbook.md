# AutoStop Remote v2 Windows Fleet

This playbook is the only detailed Manager route for AutoStop Remote v2.
It controls a Windows fleet only through the existing root-owned local
`managed-pc` CLI on MNG1. It creates no Manager API, MCP tool, public listener,
direct SSH route or new network port.

## Scope and source of truth

- Read `/opt/autostop-managed-pc/README.md` before a live fleet operation. It
  owns the CLI contract, registry state, enrollment, recovery and rollback.
- Use the v2 DNS endpoint `remote.autostopcrm.ru:22` and its pinned ED25519 host
  key only. Never use a raw-IP fallback, disable host checking, or use
  `StrictHostKeyChecking=accept-new`.
- The agent speaks only to the root-owned local `managed-pc` wrapper. It must
  not construct a direct Windows SSH command or add FastMCP, `mcp_server.py`,
  `mcp_tools.py`, HTTP routes, firewall rules or listeners.
- Keep keys, enrollment material, device-auth data, raw job output and file
  contents out of Manager memory, Git and reports.

## Mandatory device gate

1. Run `managed-pc list` and then `managed-pc fleet-health`. Both are
   observations: `list` shows registry records and `fleet-health` makes fresh,
   read-only reachability probes without starting Codex.
2. Select one exact canonical alias returned by `list`. A friendly name may
   be resolved only by an exact unique registry match and must then be replaced
   with its returned canonical alias. Never target a substring, a Windows hostname,
   device ID, tunnel/listener port, a guessed spelling or the newest
   device.
3. Immediately before each target operation run
   `managed-pc status <alias>`. The status must be fresh, match the same alias
   and show the state allowed by the requested operation. A prior list,
   fleet-health result or cached success is not a substitute.
4. The local CLI independently validates alias, freshness and state. Do not
   bypass its refusal, retry an unknown result blindly or reconstruct an SSH
   command outside that CLI.

Stop on an alias ambiguity, host-key mismatch, stale observation, unexpected
device identity, `degraded` or `offline` state, public listener or an error that
could expose a secret. `revoke` is the narrow exception: record a fresh status
attempt, require an exact owner request and let the CLI apply its deliberate
offline-revocation rule.

## Supported owner-authorized operations

After the mandatory gate, use only the matching local CLI operation. The agent
must state the canonical alias and exact scope in its result.

| Owner request | Local operation | Additional guard |
| --- | --- | --- |
| List or fleet condition | `list`, `fleet-health`, `status` | Read-only; `status` is itself the fresh check. |
| Start a Codex task | `codex-run` | Exact task/workdir/mode; one job per PC; never expose device-login data or raw result files. |
| Run a command | `powershell` or `cmd` | Exact bounded command only; do not turn it into an interactive shell. |
| Transfer a file | `copy-to` or `copy-from` | Exact source and destination; do not transfer keys, tokens, credential stores or runtime secrets. |
| Rename a device | `rename` | Exact owner instruction and independent post-change status. |
| Withdraw a device | `revoke` | Destructive; exact owner instruction, audit result and no replacement enrollment. |

For every non-read operation, perform a new `status <alias>` immediately before
dispatch and an independent status/readback afterward when the CLI supports it.
Do not infer success from transport alone. `codex-run`, PowerShell, cmd and file
transfer are owner-authorized remote actions, not background fleet maintenance.

## Explicit prohibitions

- Do not execute `scripts/codex_home_pc_bootstrap.ps1`; it is legacy home-PC
  material and is not a v2 install, migration or repair mechanism.
- Do not change the system Windows SSHD service/configuration, Windows firewall,
  LAN bindings or public exposure. v2 owns its separate loopback-only SSH
  process and its existing task contract.
- Do not edit MNG1 SSH, firewall, routes, endpoint keys or device records to
  make a probe pass. A server rollout or recovery remains a separately guarded
  infrastructure change with backup, independent rollback and a second session.
- A generic request to restart SSHD, change firewall/routes or repair legacy
  reverse SSH on a named server is server infrastructure, not a Windows-fleet
  operation. Stop and use that server's separately authorized runbook; do not
  route it through `managed-pc` or treat system SSHD as a v2 component.
- Do not use `StrictHostKeyChecking=accept-new`, `StrictHostKeyChecking=no`, a
  raw IP, direct SSH, a generic `shell`, a new public API or an unregistered MCP
  surface.

## Reporting and escalation

Report the canonical alias, requested action, fresh status outcome and a safe
result summary. Do not persist secrets, raw files, full command output, device
auth codes or private Codex result content. For a blocked status, mismatch or
security condition, stop the action and report the exact safe next step; do not
repair the Windows PC or control plane opportunistically.
