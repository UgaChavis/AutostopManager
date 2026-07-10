# AutoStop Manager Architecture

This document is the canonical architecture source. AutoStop Manager is a
headless control layer for Codex; it is not a CRM and not a user-facing
application.

## Runtime boundaries

```text
Owner / Codex
   -> CLI or FastMCP adapter
      -> routing and workflow planners
         -> durable Manager memory and run ledger (SQLite)
         -> tracked rules, route maps, catalogs, and playbooks
         -> read-only provider clients when explicitly enabled

AutoStop CRM <-> cards, clients, vehicles, orders, payments, files, board
Gmail        <-> messages, threads, labels, drafts, attachments, sent history
```

CRM and Gmail remain primary sources. Manager memory stores only durable rules,
preferences, compact verified conclusions, manager-level tasks, and resumable
operation checkpoints. Raw CRM exports, board snapshots, repair orders, email
bodies, credentials, and bulk personal data do not cross into durable Manager
storage.

## Code layers

| Layer | Responsibility | Main modules |
| --- | --- | --- |
| Adapters | Parse CLI/MCP input and expose typed operations | `cli.py`, `mcp_server.py`, `mcp_tools.py` |
| Routing | Classify intent/object/action/source/risk and select applicable knowledge | `routing.py`, `knowledge_base.py`, `context.py` |
| Orchestration | Build deterministic read/write plans and verification gates | `crm_card_action.py`, `crm_vin_parts.py`, `vin_oem_resolver.py`, `service_management.py` |
| Domain logic | Identity, parts, fluids, labor, source selection | `vehicle_identity.py`, `vin_lookup.py`, `work_pricing.py`, `fluid_maintenance.py` |
| Infrastructure | SQLite, provider HTTP clients, configuration, system inspection | `storage.py`, `catalog_clients.py`, `config.py`, `control_center.py` |
| Policy/audit | Data policy, MCP contracts, knowledge/docs/cleanup checks | `tool_contracts.py`, `system_audit.py`, `memory_curator.py`, `cleanup_audit.py` |

CLI and MCP adapters may orchestrate these layers, but domain code must not
depend on either adapter. External calls stay behind provider functions and
must be replaceable by fakes in tests. No module may silently write CRM or
Gmail data.

## Deployment model

The canonical server checkout is `/opt/AutostopManager`. Production CRM mounts
that directory and registers Manager tools inside the CRM MCP process. There is
no standalone AutoStop Manager `systemd` service in production. Local
development may start `python -m autostop_manager.mcp_server`, which is
loopback-only because it has no built-in authentication.

Restart only the CRM component that imports the mounted Manager package after
a verified deploy. The exact backup, migration, smoke, and rollback sequence is
in `docs/agent/deployment_runbook.md`.

## Contracts and change rules

- Public Python, CLI, and MCP names are compatibility surfaces.
- `autostop_manager.tool_contracts` is the machine-readable operational
  contract registry for every Manager MCP tool.
- Write planners fail closed without an exact target, current state,
  concurrency token, bounded patch, and post-write reread specification.
- SQLite schema changes are versioned migrations and must be restart-safe.
- Route selection must expose confidence and ambiguity; a broad request must
  not be forced into a narrow playbook by one shared word.
- Configuration is read at the operation boundary; tests inject environment,
  stores, clients, and clocks rather than contacting production systems.

## Dependency direction

Adapters may depend on orchestration, domain, policy, and infrastructure.
Orchestration may depend on domain interfaces and infrastructure clients.
Domain modules must remain usable without FastMCP, the CLI, live CRM, Gmail, or
network access. Infrastructure never imports adapters. The import graph is
checked for cycles during architecture review.
