# Agent Manager V2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Upgrade AutostopManager from passive memory plus document routing into an operational manager layer with contextual memory preparation, stronger knowledge routing, a skill registry, and auditable run ledgers.

**Architecture:** Keep SQLite as the local durable store and expose new capabilities through the existing CLI/MCP pattern. Add focused modules for context preparation, command routes, skill registry, and operational run tracking; keep existing memory, knowledge, and service routing behavior compatible.

**Tech Stack:** Python 3.11, SQLite, argparse CLI, FastMCP tool registration, pytest.

---

## File Structure

- Create `autostop_manager/context.py`: compose memory, rule, knowledge, and required-context output for a natural task.
- Create `autostop_manager/skill_registry.py`: load and audit local Codex skills linked to knowledge domains.
- Create `docs/agent/command_routes.json`: canonical owner command aliases such as `Приберись` and `прибейсь`.
- Modify `autostop_manager/storage.py`: add memory metadata fields and operational run ledger tables/methods.
- Modify `autostop_manager/knowledge_base.py`: add command route boosting and stopword filtering.
- Modify `autostop_manager/cli.py`: expose `prepare-context`, `skills-audit`, and `run-*` commands.
- Modify `autostop_manager/mcp_tools.py`: expose new tools through MCP.
- Modify docs/catalogs: update manager MCP catalog and startup docs for new tools.
- Add tests covering all new behavior.

## Tasks

### Task 1: Memory Orchestrator

**Files:**
- Create: `autostop_manager/context.py`
- Modify: `autostop_manager/storage.py`
- Modify: `autostop_manager/cli.py`
- Modify: `autostop_manager/mcp_tools.py`
- Test: `tests/test_context.py`

- [ ] Write failing tests for `prepare_manager_context`.
- [ ] Verify the tests fail because the module/CLI command is missing.
- [ ] Implement memory metadata and context composition.
- [ ] Expose `prepare-context` CLI and `prepare_manager_context` MCP.
- [ ] Run focused tests until green.

### Task 2: Knowledge Router V2 And Skill Registry

**Files:**
- Create: `autostop_manager/skill_registry.py`
- Create: `docs/agent/command_routes.json`
- Modify: `autostop_manager/knowledge_base.py`
- Modify: `autostop_manager/cli.py`
- Modify: `autostop_manager/mcp_tools.py`
- Test: `tests/test_knowledge_router_v2.py`
- Test: `tests/test_skill_registry.py`

- [ ] Write failing tests for command aliases, stopwords, and skill audits.
- [ ] Verify the tests fail for the expected missing behavior.
- [ ] Implement command route scoring and stopword filtering.
- [ ] Implement skill registry load/audit.
- [ ] Expose `skills-audit` CLI and MCP.
- [ ] Run focused tests until green.

### Task 3: Operational Run Ledger

**Files:**
- Modify: `autostop_manager/storage.py`
- Modify: `autostop_manager/cli.py`
- Modify: `autostop_manager/mcp_tools.py`
- Test: `tests/test_run_ledger.py`

- [ ] Write failing tests for starting, recording, finishing, and listing manager runs.
- [ ] Verify the tests fail for missing methods/commands.
- [ ] Add run ledger tables and storage methods.
- [ ] Expose `run-start`, `run-event`, `run-finish`, and `run-list` through CLI and MCP.
- [ ] Run focused tests until green.

### Task 4: Documentation And Verification

**Files:**
- Modify: `README.md`
- Modify: `docs/agent/autostop_manager_skill.md`
- Modify: `docs/agent/manager_mcp_catalog.json`
- Modify: `docs/agent/knowledge_base_index.md`
- Modify: `docs/agent/knowledge_shelves.md`

- [ ] Update docs to describe the new context, skill, command-route, and run-ledger loop.
- [ ] Run JSON validation for changed catalogs.
- [ ] Run `knowledge-sync` and `knowledge-audit`.
- [ ] Run the full pytest suite.
