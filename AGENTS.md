# AutostopManager Codex Instructions

Canonical compact startup file for Codex in `/opt/AutostopManager`.
Detailed procedures live in `docs/agent/*_playbook.md`; routing metadata lives
in `docs/agent/knowledge_map.json` and `docs/agent/command_routes.json`.
Keep docs compact.

## Role And Sources

- Answer the owner in Russian by default: short, operational and direct.
- AutostopManager stores only durable non-CRM memory: routes, rules, compact
  references, technical cursors, server checks and verified lessons.
- AutoStop CRM is the source of truth for cards, clients, vehicles, repair
  orders, payments, cashboxes, files and board state.
- AutoStop App is the source of truth for catalog, stock, batches, suppliers,
  quotes, internet orders, warehouse and marketplace state.
- Gmail is the source of truth for messages, threads, labels, drafts,
  attachments and sent/archive history.
- Telegram is the source of truth for its dialogs, messages, groups, channels,
  contacts and account authorization. Keep its private content transient.
- Never persist raw CRM/Store/Gmail/Telegram exports, customer or vehicle
  identifier tables, ledgers, credentials, OAuth state or secrets in docs,
  Git, memory, workflow state or chat summaries.
- Never dump `.env`, Docker `.Config.Env` or process environments. Inspect an
  explicit non-secret allowlist and report only presence or validation booleans.

## Startup

1. For a non-trivial request run one project-venv context command:
   `.venv/bin/python -m autostop_manager.cli agent-brief "<query>"` or
   `prepare-context "<query>"`. Never fall back to host Python.
   For a voice session, load `docs/agent/voice_agent_brief.md` before the
   first owner task. Treat a direct owner voice command as authority to resolve
   ordinary operational details and complete its intended result; verification
   gates are internal execution steps, not a request for another confirmation.
2. For local docs/knowledge work run `knowledge-probe "<query>"`, then open
   `open_first` and only the returned sources before broader reads.
3. Resolve `work` or `learning` mode. In learning mode use the
   `autostop-learning-loop` skill and finish its post-run review.
4. When the owner starts `режим директора`, `директор автосервиса`, or a
   recurring director goal, load `.agents/skills/run-autostop-director/SKILL.md`
   and `docs/agent/service_director_manifest.md` before live reads.
   Read `today_context` and the active unified director-journal entries before
   the live board scan. Reconcile pending decisions, questions and scheduled
   reviews with live CRM before selecting new work; follow the manifest for
   context, privacy, write and retention rules.
5. For multi-step work use the Gateway v2 workflow ledger with compact,
   state-versioned checkpoints. Raw discovery is only for a capability not
   covered by a named workflow; a raw write requires its exact literal name,
   live schema hash and unique idempotency key.

## Live Systems

- CRM: use the 24-tool Gateway v2 connector. Start with `agent_bootstrap` and
  `agent_board_digest`, then `agent_search` and `agent_entity_context`.
  Broad control uses named `agent_board_workflow` operations. The only guarded
  raw new-card exception is documented in
  `docs/agent/crm_manager_data_playbook.md`.
- Store: AutoStop App remains authoritative, but Store work is paused while it
  is under development. Do not inspect, diagnose or change it until the owner
  explicitly reauthorizes Store work. After reauthorization, open
  `docs/agent/store_management_playbook.md`; never read the Store DB or treat a
  customer order as a supplier purchase.
- Gmail: open `docs/agent/gmail_workflow_playbook.md`; search/read before a
  mailbox mutation and keep only refs in the workflow ledger.
- Telegram: open `docs/agent/telegram_workflow_playbook.md`; use the private
  local bridge, resolve exact peer IDs, and keep reads bounded and writes
  owner-authorized, idempotent and independently verified.

The production connector exposes exactly 24 Gateway v2 tools. Codex/Apps use
owner-approved OAuth 2.1 with PKCE and rotating refresh tokens; the rotated
bearer is internal compatibility only.

## Automotive Work

- Follow the returned knowledge route and select sources for the actual fact:
  focused CRM vehicle/card context, Store catalog/stock/price, VIN/OEM
  applicability, official public communications, licensed service data or
  public research. Final safety, procedure, torque, fluid, programming and exact
  fitment facts require an appropriate vehicle/unit-specific OEM or licensed
  source.
- VIN by Russian registration number, part/cross and labor tasks route through
  `vehicle_identity_playbook.md`, `partsapi_method_contracts.md` and, when
  applicable, `work_labor_pricing_playbook.md`. A plate-derived VIN is an
  identity lead that must be checked against the vehicle/CRM before use.
- Applied estimates (`процени`, `распиши ЗН`, complaint-to-estimate) use the
  `resolve-autostop-service-case` skill. Labor pricing reconciles the
  aggregate-only `service_labor_experience.json`, current labor-only market
  and vehicle-specific labor time; high confidence needs exact scope and three
  independent evidence families. AUTONORMS `workTime` is evidence, not price.
- `research_drive2_cases` supplies bounded public hypotheses only. Never
  bypass login, CAPTCHA, paywall or IP blocks, and never use forums as final
  authority for procedure, safety or fitment.

## Write Safety

- A direct owner task authorizes the necessary non-financial changes and the
  homogeneous target selection reasonably required to achieve its stated
  outcome. Resolve operational details from focused live reads and act without
  asking for routine criteria; do not expand into unrelated cleanup.
- Mandatory order: focused reread -> `prepare_action_contract` -> named
  workflow `dry_run` -> `apply` -> exact-target reread and verification.
  Use idempotency, concurrency controls and a correlation id; unresolved
  outcomes remain `compensating`.
- Payments, cashboxes, refunds, payroll, supplier orders and any financial-total
  change require a direct owner instruction for that exact operation.
- Card description or vehicle-profile writes use
  `agent_board_workflow(operation="cleanup_card")` in dry-run and apply modes.
  Follow the single canonical text model in
  `docs/agent/crm_card_description_standard.md`; do not duplicate its style or
  content rules here. When the current state changes, update `description` and
  `board_summary` together.
- Move, archive, delete, deadline/indicator changes and repair-order edits must
  be necessary to the active task and pass the same safeguards.
- Finance, inventory, documents, files, Gmail and destructive writes require an
  action contract, unique idempotency and exact reconciliation.

## Standing Routes

- Director mode / recurring workshop goal ->
  `.agents/skills/run-autostop-director/SKILL.md` and
  `docs/agent/service_director_manifest.md`.
- `Приберись` -> `docs/agent/board_cleanup_autopilot_playbook.md`.
- CRM descriptions -> `docs/agent/crm_card_description_standard.md`.
- `ready unpaid` / daily control ->
  `docs/agent/krasnoyarsk_service_management_playbook.md`.
- Timer floor -> `docs/agent/crm_manager_data_playbook.md`, then
  `bulk_set_deadline_if_below` dry-run before apply.
- VIN/OEM CRM writeback ->
  `docs/agent/crm_vin_oem_parts_lookup_playbook.md`.
- Business documents ->
  `docs/agent/business_document_quality_playbook.md`.
- After explicit Store reauthorization: state and operations ->
  `docs/agent/store_management_playbook.md`; analytics ->
  `docs/agent/store_analytics_playbook.md` and aggregate
  `get_store_analytics_report`.
- Public repair research -> `docs/agent/automotive_repair_source_playbook.md`;
  use Gateway search/excerpt/browser routes without bypassing access controls.
- Telegram messages, contacts, groups, attachments, voice messages, QR login
  or connection checks ->
  `.agents/skills/manage-owner-telegram/SKILL.md` and
  `docs/agent/telegram_workflow_playbook.md`.
- public Krasnoyarsk camera / street / address / landmark -> resolve the strict
  allowlist through `.agents/skills/capture-public-camera/SKILL.md` and capture
  one frame via `scripts/capture_public_camera.py`; retain no video, player
  URLs, frames, faces or plates. `scripts/capture_semafornaya_185.py` remains
  the AutoStop compatibility route.
- `домашняя камера` / `Tapo C225` -> explicit owner-requested photo, short
  silent clip, or bounded PTZ/vehicle scan via
  `docs/agent/home_camera_playbook.md`, `scripts/capture_home_camera.py`, and
  `scripts/control_home_camera_ptz.py`; never schedule, publish, identify
  people, continuously track, or retain the private stream.
- Server/Windows -> `docs/agent/codex_home_pc_reverse_ssh.md`. For FST.KZ read
  `/root/.codex/CODEX_VPN_FST_ACCESS.md` first and use `autostop-vpn-fst`.
  Resolve live identity and stop on host-key mismatch.
- Reception PDF printing -> `docs/agent/codex_home_pc_reverse_ssh.md`.

## Git, Deploy And Docs

- Publish from the workstation with `git push origin HEAD:AutostopManager`;
  never force-push production or use the production server as normal publisher.
  `gh auth status` is not required for Git push.
- Follow `docs/agent/deployment_runbook.md` for revision checks, deploy and
  rollback.
- Keep one canonical owner per rule, link instead of copying, update the
  smallest existing file and remove migrated, duplicate or dated text.
- Run `cleanup-audit` before deleting tracked/generated artifacts. After docs,
  routes, catalogs or skills change run `knowledge-sync`, `knowledge-audit`,
  `annotations-audit` and `skills-audit`.

Core references: `README.md`, `docs/agent/manager_rules.json`,
`docs/agent/command_routes.json`, `docs/agent/knowledge_shelves.md`,
`docs/agent/manager_mcp_catalog.json`, `docs/agent/crm_mcp_catalog.json`.
