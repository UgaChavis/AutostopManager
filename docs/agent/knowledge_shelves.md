# Knowledge Shelves

Run `agent-brief` for workflows, then `knowledge-probe` for documents.
Knowledge never grants connector or write authority.

| Shelf | Domains | Open first |
|---|---|---|
| Startup and knowledge | `startup_and_identity`, `knowledge_intake` | `AGENTS.md`, `knowledge_map.json` |
| CRM and service | `service_management`, `service_director`, `board_cleanup_autopilot` | returned playbook or skill |
| Business/files | `business_identity`, `business_documents`, `gmail_operations` | returned playbook |
| Vehicle and parts | `vehicle_identity_and_oem`, `crm_vin_oem_parts_lookup`, `parts_sourcing`, `work_labor_pricing` | returned playbook |
| Repair | `automotive_repair`, `bmw_repair`, `toyota_gr_yaris`, `fluids`, `transmission`, `ecu_calibration_programming` | returned playbook |
| Devices and access | `remote_codex_access`, `public_camera`, `home_camera`, `telegram_operations` | returned playbook or skill |
| Store *(paused)* | `store_management`, `store_analytics_reporting` | only after explicit reauthorization |
| Release | `deployment`, `ecosystem_capability_parity` | `deployment_runbook.md` |

Ownership is strict:

- `command_routes.json`: operational recognition and workflows.
- `knowledge_map.json`: domain-to-file navigation only.
- playbooks and skills: procedures.
- live registration: schemas; MCP catalogs are manifests.
- private data, business records, caches and generated files stay untracked.

Prefer updating an existing canonical file. Delete a tracked document only
after unique rules are migrated, navigation is updated and `cleanup-audit`
finds no dependency. Then run `knowledge-sync`, `knowledge-audit`,
`annotations-audit` and `skills-audit`. The annotation command is a
compatibility metadata audit, not a routing layer.
