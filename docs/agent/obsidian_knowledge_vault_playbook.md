# Obsidian Knowledge Vault Playbook

Purpose: make the AutoStop Obsidian vault a regular working knowledge layer
for the manager agent without replacing CRM, Gmail, or AutostopManager indexes.

## Vault Paths

Primary cloud vault:

- `C:\Users\User\Мой диск\Obsidian CRM\AutostopCRM`

Local desktop mirror:

- `C:\Users\User\Desktop\Obsidian CRM\AutostopCRM`

Treat the cloud vault as primary. Treat the desktop mirror as a secondary
cache for local access, not as a separate source of truth.

Open-first notes:

- `Home.md`
- `80_Codex\Codex interaction.md`
- `10_Manager\CRM\CRM manager workspace.md`
- `30_Knowledge\AutoStopManager\docs_agent\knowledge_base_index.md`
- `30_Knowledge\AutoStopCRM-V1\README.md`
- `30_Knowledge\AutoStopCRM-V1\docs\OPERATIONS_RUNBOOK.md`
- `00_Home\Bases\Knowledge.base`

## Role

Use Obsidian as the human-facing operating knowledge base:

- readable playbooks, notes, and source-pack navigation;
- Bases and tags for browsing domains;
- manager notes that should be easy for the owner to inspect on another PC;
- short synthesized CRM/MCP/connector operating guidance.
- safe CRM manager snapshots: board load, cashbox overviews, repair-order
  counts, client-data quality signals, and shared-file metadata.
- short playbooks, indexes, route cards, annotations, and safe summaries.

Do not use Obsidian as the source of truth for live CRM state, Gmail threads,
cashboxes, client data, repair orders, or secrets.

Do not import source-pack PDFs by default when a Markdown, JSON, CSV, or JSONL
equivalent exists. Keep PDFs as cold/archive references unless the owner asks
to inspect rendered pages.

## CRM Manager Data Layer

Use `crm_manager_data_playbook.md` before putting live CRM-derived information
into Obsidian.

Obsidian may store safe summaries and dated snapshots:

- board counts, column load, and attention cards by short id;
- cashbox balances and total income/expense by cashbox;
- repair-order counts, unpaid totals, and inconsistent order counts;
- client-quality signals such as duplicate-key counts or missing structured
  names;
- metadata for CRM shared files such as `Клиенты.xls`.

Raw client lists, phone rows, VIN/license-plate databases, full cash journals,
full repair-order text, and full board dumps stay in AutoStop CRM unless the
owner explicitly approves that exact export to the cloud vault.

## Default Agent Workflow

For non-trivial AutoStop manager work:

1. Use `prepare_manager_context` or `probe_knowledge_base` first when available.
2. Prefer `agent_brief` before broad document reads when starting a new agent
   task.
3. If the task needs human-readable instructions, source-pack browsing, or a
   durable note, inspect the Obsidian vault with `rg` before broad reads.
4. Prefer the cloud vault path when it exists. Fall back to the desktop mirror
   only when the cloud path is unavailable.
5. Open `Home.md` first, then the relevant imported playbook or Base.
6. Keep canonical route changes in `docs/agent` first, then refresh the
   Obsidian import.
7. Store only short durable conclusions in manager memory.

## Refreshing The Vault

When `docs/agent` changes and Obsidian should reflect it:

```powershell
powershell -ExecutionPolicy Bypass -File "C:\Users\User\Desktop\Obsidian CRM\AutostopCRM\_system\setup_autostop_obsidian_vault.ps1"
```

If working from the cloud vault, run the same script from the cloud copy:

```powershell
powershell -ExecutionPolicy Bypass -File "C:\Users\User\Мой диск\Obsidian CRM\AutostopCRM\_system\setup_autostop_obsidian_vault.ps1"
```

The setup script imports `C:\Users\User\Desktop\AutoStopManager\docs\agent`
into `30_Knowledge\AutoStopManager\docs_agent` and adds Obsidian properties to
Markdown copies.

The same script also imports canonical AutoStopCRM-V1 Markdown documents into
`30_Knowledge\AutoStopCRM-V1` so CRM/MCP/connector docs can be browsed from the
same vault.

## Cloud Sync

The Google Drive folder `C:\Users\User\Мой диск` is the cross-PC sync layer.
Use the cloud vault for work that must appear on the work PC.

Before editing the same note from multiple machines:

- let Google Drive finish syncing;
- avoid editing the same note simultaneously on two PCs;
- check for conflict copies before trusting a note;
- keep `.obsidian` synced with the vault so Bases, templates, and core plugin
  settings follow the vault.

## Safety Boundaries

Do not write these into Obsidian notes:

- full CRM board exports or raw card dumps;
- full client databases, phone lists, or cashbox ledgers;
- raw Gmail threads or attachments unless the owner explicitly asks to archive
  a safe excerpt;
- secrets, API keys, bearer tokens, OAuth data, or production env files;
- copied licensed manuals or full commercial databases.

For sensitive facts, store only the route to the source and a short reusable
decision rule.

## Useful Searches

```powershell
rg -n "MCP|connector|bootstrap_context|tools/list" "C:\Users\User\Мой диск\Obsidian CRM\AutostopCRM"
rg -n "Приберись|board_summary|archive|deadline" "C:\Users\User\Мой диск\Obsidian CRM\AutostopCRM"
rg -n "BMW|N63|BDC|ISTA|xDrive" "C:\Users\User\Мой диск\Obsidian CRM\AutostopCRM"
rg -n "закупочная цена|ZZap|Drom|наличие" "C:\Users\User\Мой диск\Obsidian CRM\AutostopCRM"
```
