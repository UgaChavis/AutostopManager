# Intelligent AutoStop Manager and Learning Mode

## Purpose

AutoStopManager is an orchestration, memory, routing, and verification layer.
It does not replace AutoStop CRM, AutoStop App, or Gmail as sources of truth.
For a mixed owner request, choose evidence by the claim being answered rather
than by a rigid script: CRM for live service facts, Store for internal catalog
and stock, VIN/EPC for exact applicability, suppliers for procurement, and
public technical sources or forums for research hypotheses.

Return one working recommendation: selected action or part, reason, relevant
alternatives, current business evidence, and the next useful action. Do not
turn an internal source conflict into a long user-facing search diary unless it
changes the decision.

## Execution Modes

`work` is the default. Read relevant memory and proven paths, execute the
request adaptively, verify normal writes, and answer without a mandatory
learning cycle.

`learning` adds a mandatory post-run cycle. The global mode may be changed by
the owner and a single task may override it. Per-task mode wins over the global
setting. `agent_bootstrap` is authoritative for the effective mode.

Use `agent_mode(action="get"|"set"|"resolve")` or the local CLI commands
`agent-mode status`, `agent-mode set work|learning`, and `learning-summary` to
operate the switch. In learning mode `agent_bootstrap` creates or reuses a
hash-only active turn even when a local Codex hook is unavailable.

Use the project `autostop-learning-loop` skill whenever the effective mode is
`learning` or the lifecycle hook asks to finish a learning cycle.

## Learning Cycle

1. Build or reuse a safe task signature and retrieve relevant durable lessons.
2. Call `agent_case_resolver(operation="plan")`, execute its source-owned
   read plan, then call `agent_case_resolver(operation="reconcile")` with
   transient scalar evidence. It has no apply mode and never writes data.
3. Verify the result with source-specific completion checks and exact readback
   for a write.
4. Call `post_run_review` before the final response. Store only technical tool
   status, sanitized refs, completion evidence, and a reusable conclusion.
5. When a local defect is reproducible, repair the smallest responsible layer,
   add a regression test, verify it, then rerun the affected step.
6. Close the cycle through `agent_learning_workflow`. The Stop hook permits the
   final response after a completed review with no candidate, or after every
   candidate is `promoted`, `deferred`, or `rolled_back`.

The model's self-assessment is a candidate, not a fact. Promote a durable
lesson only after a direct owner signal, repeated verified success, or a
reproduced defect with a verified regression fix. Archive or review conflicting
lessons instead of blending them into a vague rule.

## Repair Decisions

Repair before answering only when the issue is local, reproducible, reversible,
testable, and fits the configured learning budget. Use one isolated repair
branch/worktree and one bounded Stop continuation. A failed verification must
roll back the repair and mark the candidate accordingly.

Treat provider outage, rate limit, CAPTCHA, missing contract, or unavailable
licensed evidence as an external/deferred issue. Use the best safe fallback;
do not generate speculative code changes.

Changes to tracked instructions, adapters, Manager, CRM, or Store require the
appropriate tests, knowledge audits, deployment checks, and rollback evidence.
Dynamic reusable lessons may take effect immediately in Manager memory; promote
them into tracked instructions only when the evidence threshold is met.

## Safety and Privacy

An active owner task authorizes the non-financial, exact-target changes needed
to finish that task after normal preflight and reread verification. It does not
authorize unrelated cleanup or silent scope expansion.

Payments, cashbox entries, refunds, payroll payouts, supplier orders, and any
change to a financial total require a direct owner instruction for that exact
operation. A self-repair workflow never creates a financial business action.

Never put raw prompts, CRM records, Store rows, Gmail messages, VINs, client
contacts, money values, credentials, or full tool payloads into learning memory
or hook output. Store source-system facts in their source system and keep only
minimal technical references in the Manager ledger.

The project hook file is `.codex/hooks.json`. After a checkout changes it,
explicitly trust the project hooks in Codex through `/hooks`; until then Codex
skips command hooks by design. The hook fails open if local storage is not
available, records only hashes/status shapes, blocks one unreviewed learning
turn, and defers it on the second Stop continuation to avoid a loop.

## Case Resolver and Evidence

For a mixed task, decompose the answer into typed claims. Build a read-only DAG
whose nodes declare source, purpose, freshness, risk, expected value, fallback,
and dependencies. Run independent pure reads in parallel only where provider
limits permit.

Record each fact in an EvidenceBundle with claim type, source kind, retrieval
time, applicability, confidence, and conflict state. OEM reference, selected
part, cross, stock, procurement price, retail benchmark, repair hypothesis, and
forum observation are separate claims. Use OEM/licensed sources for final
safety, torque, fluid, programming, or exact-fitment facts.

## Completion Checks

- `work`: no learning review is required.
- `learning`: every agent turn has a closed review status and no raw business
  data in experience rows.
- A repaired tool has a regression test, verification evidence, and rollback
  reference.
- Public CRM Gateway remains exactly 24 tools; new Manager capabilities remain
  hidden behind the Manager/raw route.
- Run `knowledge-sync`, `knowledge-audit`, `annotations-audit`, `skills-audit`,
  and `cleanup-audit` after durable documentation or skill changes.
