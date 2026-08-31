---
name: remote-diagnostics-pad-vii
description: Prepare and run a supervised Launch PAD VII remote diagnostic session through the isolated AutoStop Remote gateway.
---

# Launch PAD VII remote diagnostics

Use this skill for staged remote vehicle diagnostics with AutoStop Remote.

Before any live tablet call, read in order:

1. `docs/agent/remote_diagnostics_pad_vii_playbook.md`
2. `/opt/autostop-remote-diagnostics-server/docs/REMOTE_DIAGNOSTICS_PAD_VII.md`
3. `/opt/autostop-remote-diagnostics-server/docs/LAUNCH_LIVE_RUNBOOK.md`

The `autostop_remote_diagnostics` MCP is the only tablet action plane. Never
copy it into Manager/CRM, expose its UDS/env, use ADB, shell, raw Intent,
UIAutomator, arbitrary packages, queues, batches or retries.

Do not call status, observe, history or action until the owner explicitly starts
the live session. Then require metrics-confirmed READY and the full current
status gate. Use fresh observe → at most one action → fresh observe, with a
fresh screenshot-free observe by default. Keep all raw diagnostic evidence
transient. Stop on stale state, unknown UI/outcome, terminal error, a changing
screen, or any prohibited automotive operation.
