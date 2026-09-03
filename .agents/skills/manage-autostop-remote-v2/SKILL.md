---
name: manage-autostop-remote-v2
description: Безопасно управлять парком Windows через AutoStop Remote v2: список, свежий статус, запуск одной задачи Codex, PowerShell/cmd, передачу файла, переименование или отзыв по точному поручению владельца.
---

# Управление AutoStop Remote v2

Before any managed-PC operation, read
`docs/agent/autostop_remote_v2_playbook.md` and
`/opt/autostop-managed-pc/README.md` completely. They are the only owners of
the v2 control-plane contract.

## Required sequence

1. Use only the root-owned local `managed-pc` CLI on MNG1. Do not construct an
   SSH command, create a Manager API/MCP tool or open a port.
2. Run `managed-pc list`, then `managed-pc fleet-health`. Resolve one exact
   canonical alias returned by `list`; never use a substring, hostname,
   friendly-label guess, device ID or listener port as a target.
3. Run a new `managed-pc status <alias>` immediately before every `codex-run`,
   `powershell`, `cmd`, `copy-to`, `copy-from`, `rename` or `revoke`. The CLI's
   independent freshness/state validation is authoritative.
4. Use only the owner-authorized operation, then perform the documented safe
   readback. A Codex job is one-at-a-time and must not reveal login data or raw
   result files.

## Stop conditions

- Stop on ambiguity, stale/non-online state, a host-key mismatch, unexpected
  identity, public listener or possible secret exposure. `revoke` needs an
  exact owner request and follows the CLI's explicit offline rule.
- Never use raw-IP fallback, `StrictHostKeyChecking=accept-new`, disabled host
  checking, direct SSH, the legacy
  `scripts/codex_home_pc_bootstrap.ps1`, system Windows SSHD/firewall changes,
  a generic interactive shell, FastMCP or an unregistered MCP surface.
- Never repair, enroll, re-enroll, rotate, deploy or modify MNG1 networking as
  a side effect of a fleet task. Escalate using the v2 playbook instead.
