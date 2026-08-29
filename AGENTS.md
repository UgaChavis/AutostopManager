# AutostopManager Codex Instructions

Compact startup contract for work in `/opt/AutostopManager`. Detailed
procedures belong to the playbooks returned by the route, not here.

## Startup

- Answer the owner in Russian by default: short, operational and direct.
- For a non-trivial request use the project venv once:
  `.venv/bin/python -m autostop_manager.cli agent-brief "<query>"`. Never fall
  back to host Python. Use `knowledge-probe` only for focused document lookup;
  it never grants writes, connector access or financial authority.
- Resolve `work` or `learning` mode. In learning mode use the
  `autostop-learning-loop` skill; ordinary work has no mandatory learning
  review.
- For a voice session read `docs/agent/voice_agent_brief.md` before the first
  owner task. For multi-step work use the Gateway v2 workflow ledger with
  compact, state-versioned checkpoints.

## Source Boundaries

- AutostopManager stores only durable non-CRM memory: routes, cross-system
  rules, compact references, technical cursors and verified lessons.
- AutoStop CRM is the source of truth for cards, companies, clients, vehicles,
  repair orders, payments, cashboxes, files and board state.
- AutoStop App is the source of truth for Store catalog, stock, batches,
  suppliers, quotes, orders, warehouse and marketplace state.
- Gmail is the source of truth for messages, threads, drafts, labels,
  attachments and sent history. Telegram is the source of truth for its own
  dialogs, contacts and media; keep private content transient.
- Never persist raw CRM/Store/Gmail/Telegram exports, customer or vehicle
  identifiers, mail/chat bodies, money ledgers, credentials, OAuth state or
  secrets in docs, Git, Manager memory or workflow state.
- Never dump `.env`, Docker `.Config.Env` or process environments. Inspect an
  explicit non-secret allowlist and report only presence or validation flags.

## Remote Vehicle Diagnostics

- AutoStopManager now owns the operator workflow for supervised remote vehicle
  diagnostics: it plans a run, controls the diagnostic tablet through the
  isolated `/opt/autostop-remote-diagnostics-server` gateway, interprets
  evidence, prepares owner-facing reports and improves the workflow from
  verified experience.
- Treat diagnostic interfaces as variable. Work adaptively from the current
  screen, accessibility tree, optional visual evidence and observed effects;
  prefer creative evidence-driven strategies over rigid click-by-click routes.
  Optimize for speed, but verify that each dispatched action produced the
  intended screen or diagnostic result.
- The gateway and Android client remain separately versioned technical
  components. Manager may improve its own operator playbooks and tools, and may
  prepare precise evidence-backed handoff prompts for the server or Android
  Codex agent when a client/protocol change is needed.
- Raw screenshots, UI trees and diagnostic values never belong in Git, Manager
  memory or workflow state. If retention is enabled by a separate owner
  decision, Manager owns their lifecycle in protected private runtime storage
  outside the repository; durable knowledge keeps only safe references,
  metadata and verified reusable lessons.
- Read-only identification, DTC, freeze-frame and parameter work may be composed
  flexibly within the owner's live scope. Clearing DTCs, active tests, resets,
  service functions, coding, adaptation, calibration, flashing and immobilizer
  operations still require direct authorization for that exact operation.

## Store Pause

Store work is paused while AutoStop App is under development. Do not inspect,
diagnose or change Store until the owner explicitly reauthorizes it. This pause
does not permit an automatic Store read during CRM bootstrap; an explicit
Store request remains separately scoped.

## Live Work And Write Gates

- CRM's only external Codex surface is the configured 24-tool Gateway v2 connector.
  Start with `agent_bootstrap` and `agent_board_digest`, then use focused search
  and exact entity context. The standalone 77-tool Manager registry is internal
  project/runtime inventory, not another configured Codex or App surface;
  production imports only the Manager subset required by the 24-tool Gateway.
  Treat `codex_apps/autostopcrm.*` and any unconfigured account App namespace as
  legacy, outside project health checks and execution paths.
- A direct owner task authorizes only the necessary in-scope non-financial
  changes. Do not expand it into unrelated cleanup, Store work or deployment.
- Every mutation follows: focused exact reread -> `prepare_action_contract` ->
  named workflow `dry_run` -> `apply` with a different idempotency key ->
  independent exact reread. Use concurrency controls and a correlation id;
  unresolved outcomes remain compensating.
- Payments, cashboxes, refunds, payroll, supplier orders, repair-order totals
  and other financial changes require a direct instruction for that exact
  operation. Destructive changes require the same exact scope.
- Document and Gmail delivery contracts keep only refs, hashes and booleans.
  A PDF invoice send requires the exact recipient, verified sender, attachment
  SHA-256 and successful document QA; a financial or tax mismatch requires
  separate confirmation.
- Raw discovery is only for a capability absent from a named workflow. A raw
  write requires its exact literal name, current schema hash, unique
  idempotency key and strict readback.

## Server And Release Boundary

- For FST.KZ first read `/root/.codex/CODEX_VPN_FST_ACCESS.md`, use
  `autostop-vpn-fst`, never expose secrets, and never route CRM through VPN.
- Preserve dirty work. Never reset, rebase, force-push, delete a release or
  restart services unless the active owner request explicitly requires it.
- Publish Manager from the workstation with
  `git push origin HEAD:AutostopManager`. Use
  `docs/agent/deployment_runbook.md` for revision checks, deployment and
  rollback. A release-preparation task does not authorize deployment.
- Keep one canonical owner per rule and link instead of copying. After docs,
  routes, catalogs or skills change run `knowledge-sync`, `knowledge-audit`,
  `skills-audit` and `cleanup-audit` before the normal
  lint, type, test and coverage gates.

Navigation owners: `docs/agent/command_routes.json` for operational workflows,
`docs/agent/knowledge_map.json` for document paths,
`docs/agent/manager_rules.json` for cross-system runtime invariants, and the
live Manager/CRM registrations for tool schemas.
