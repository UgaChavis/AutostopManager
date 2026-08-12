# Reception PDF Printing

Canonical route for printing an owner-approved PDF on the AutoStop reception
printer from an AutoStopManager chat task.

## Route

```text
AutoStopManager chat
  -> Manager/CRM VPS
  -> managed-pc pinned reverse SSH
  -> desktop-e0e84lt (DESKTOP-E0E84LT)
  -> HUAWEI PixLab X1
```

The FST.KZ VPN server is not part of this route. Keep its access and credentials
independent and never route CRM or reception-PC traffic through that VPN.

## Command

Use the named control-plane command:

```bash
managed-pc print-pdf desktop-e0e84lt \
  --file /absolute/path/document.pdf \
  --printer "HUAWEI PixLab X1" \
  --copies 1
```

The command accepts a local PDF, verifies the live managed-PC channel, renders
the document on the server, transfers only the rendered pages, and submits an
A4 black-and-white one-sided Windows print job. The printer is allowlisted;
one copy is the default.

## Chat Route

Treat requests such as `распечатай последний PDF на ресепшене` and
`распечатай документ на HUAWEI PixLab X1` as this route. Resolve the exact local
PDF and require an explicit owner instruction to print; a request to inspect or
discuss printing does not authorize a print job.

Before execution, read `docs/agent/codex_home_pc_reverse_ssh.md` and resolve the
current exact device identity. Stop if the alias, hostname, tunnel, SSH check,
local file, or allowed printer does not match.

## Completion

Report the exact PDF filename, `desktop-e0e84lt`, `HUAWEI PixLab X1`, copies,
pages, and whether Windows accepted the job. Do not claim physical delivery
when only spool acceptance was observed, and do not repeat an ambiguous job
without a new owner instruction.
