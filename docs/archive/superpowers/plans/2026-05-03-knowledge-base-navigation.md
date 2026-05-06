# Knowledge Base Navigation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a minimal AutostopManager knowledge-base navigation layer backed by SQLite, CLI, and MCP tools.

**Architecture:** `knowledge_map.json` remains the source for domains and routes. A new Python module indexes routed Markdown/JSON/Codex skill files into SQLite document and section tables, then searches those tables before the agent reads full files.

**Tech Stack:** Python standard library, SQLite, argparse, existing MCP registration pattern, pytest.

---

### Task 1: Storage And Indexing API

**Files:**
- Create: `autostop_manager/knowledge_base.py`
- Modify: `autostop_manager/storage.py`
- Test: `tests/test_knowledge_base.py`

- [ ] Write failing tests for syncing `knowledge_map.json`, indexing Markdown headings, searching `BMW F15`, and routing `engine_oil`.
- [ ] Run `python -m pytest tests/test_knowledge_base.py -q` and verify failure because the module does not exist.
- [ ] Add SQLite tables `knowledge_documents` and `knowledge_sections`.
- [ ] Implement `sync_knowledge_base(store)` and `search_knowledge_base(store, query, domain=None, limit=10)`.
- [ ] Run `python -m pytest tests/test_knowledge_base.py -q` and verify pass.

### Task 2: CLI And MCP Tools

**Files:**
- Modify: `autostop_manager/cli.py`
- Modify: `autostop_manager/mcp_tools.py`
- Modify: `autostop_manager/mcp_server.py`
- Test: `tests/test_cli.py`
- Test: `tests/test_mcp_tools.py`

- [ ] Write failing parser/tool tests for `knowledge-sync`, `knowledge-search`, `sync_knowledge_base`, and `search_knowledge_base`.
- [ ] Run focused tests and verify failure.
- [ ] Wire CLI commands to the indexing API.
- [ ] Register MCP tools and update server instructions to prefer knowledge search before broad reading.
- [ ] Run focused tests and verify pass.

### Task 3: Documentation And Catalogs

**Files:**
- Modify: `README.md`
- Modify: `docs/agent/manager_mcp_catalog.json`
- Modify: `docs/agent/operating_playbook.json`
- Modify: `docs/agent/knowledge_base_index.md`

- [ ] Update docs to describe the knowledge navigation commands.
- [ ] Parse changed JSON files.
- [ ] Run the full pytest suite.

### Task 4: Route Cards, Probe, And Audit

**Files:**
- Modify: `autostop_manager/knowledge_base.py`
- Modify: `autostop_manager/storage.py`
- Modify: `autostop_manager/cli.py`
- Modify: `autostop_manager/mcp_tools.py`
- Modify: `autostop_manager/mcp_server.py`
- Modify: `docs/agent/knowledge_map.json`
- Modify: `docs/agent/knowledge_base_index.md`
- Modify: `docs/agent/autostop_manager_skill.md`
- Modify: `docs/agent/manager_rules.json`
- Modify: `docs/agent/operating_playbook.json`
- Modify: `docs/agent/manager_mcp_catalog.json`
- Test: `tests/test_knowledge_base.py`
- Test: `tests/test_cli.py`
- Test: `tests/test_mcp_tools.py`

- [x] Write failing tests for `probe_knowledge_base`, `audit_knowledge_base`, `knowledge-probe`, `knowledge-audit`, and MCP registration.
- [x] Verify RED with `python -m pytest tests/test_knowledge_base.py tests/test_cli.py tests/test_mcp_tools.py -q`.
- [x] Add `knowledge_route_cards` and build compact cards from `knowledge_map.json`.
- [x] Implement `probe_knowledge_base(store, query, limit=5)` returning `has_knowledge`, `best_domain`, `open_first`, and `source_of_truth`.
- [x] Implement `audit_knowledge_base(store)` checking route cards, mapped files, document counts, and section counts.
- [x] Wire CLI commands and MCP tools.
- [x] Add route-card metadata: `aliases`, `keywords`, `questions`, and `source_of_truth_files`.
- [ ] Run JSON validation, sync, probe smoke checks, focused tests, and the full pytest suite.
