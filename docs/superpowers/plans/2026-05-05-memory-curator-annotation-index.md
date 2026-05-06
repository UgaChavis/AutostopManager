# Memory Curator and Knowledge Annotation Index Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Improve AutostopManager memory quality, recall speed, and knowledge routing confidence with compact knowledge annotations and a curator for durable memory.

**Architecture:** Add a sidecar annotation index that is synced into SQLite beside existing route cards and sections. Add a memory curator layer that uses SQLite FTS5 when available, ranks memory by text relevance plus operational metadata, and audits stale, duplicate, expired, and superseded memories.

**Tech Stack:** Python standard library, SQLite/FTS5, pytest, existing AutostopManager CLI/MCP patterns.

---

### Task 1: Knowledge Annotation Index

**Files:**
- Create: `docs/agent/knowledge_annotations.jsonl`
- Modify: `autostop_manager/storage.py`
- Modify: `autostop_manager/knowledge_base.py`
- Modify: `autostop_manager/cli.py`
- Modify: `autostop_manager/mcp_tools.py`
- Test: `tests/test_knowledge_annotations.py`

- [ ] **Step 1: Write failing tests**

```python
from autostop_manager.knowledge_base import (
    audit_knowledge_base,
    probe_knowledge_base,
    search_knowledge_base,
    sync_knowledge_base,
)
from autostop_manager.storage import ManagerMemoryStore


def test_sync_indexes_knowledge_annotations(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")

    result = sync_knowledge_base(store)

    assert result["annotations_indexed"] > 0
    audit = audit_knowledge_base(store)
    assert audit["annotations_indexed"] == result["annotations_indexed"]
    assert audit["warnings"] == []


def test_annotation_boost_routes_memory_quality_queries(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    sync_knowledge_base(store)

    result = probe_knowledge_base(store, "улучшить память индексацию аннотации качество знаний", limit=5)

    assert result["has_knowledge"] is True
    assert result["best_domain"] == "knowledge_intake"
    assert result["confidence"] >= 0.45
    assert any("knowledge_annotations.jsonl" in path for path in result["source_of_truth"])


def test_search_uses_annotations_for_compact_answers(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    sync_knowledge_base(store)

    result = search_knowledge_base(store, "устаревшие воспоминания дубли качество памяти", limit=5)

    assert result["items"]
    assert result["items"][0]["document_type"] == "annotation"
    assert result["items"][0]["domain"] == "startup_and_identity"
```

- [ ] **Step 2: Run tests and confirm they fail because annotation support is missing**

Run: `python -m pytest tests/test_knowledge_annotations.py -q`

Expected: FAIL with missing keys/tables or missing annotation matches.

- [ ] **Step 3: Implement annotation storage and sync**

Add `knowledge_annotations` table, parse `docs/agent/knowledge_annotations.jsonl`, insert annotation rows, and include annotation counts in sync/audit output.

- [ ] **Step 4: Use annotations in probe/search**

Boost route-card scoring from matching annotation text and return annotation rows as compact `document_type="annotation"` search results.

- [ ] **Step 5: Verify focused tests pass**

Run: `python -m pytest tests/test_knowledge_annotations.py tests/test_knowledge_base.py -q`

Expected: PASS.

### Task 2: Memory Curator v1

**Files:**
- Create: `autostop_manager/memory_curator.py`
- Modify: `autostop_manager/storage.py`
- Modify: `autostop_manager/cli.py`
- Modify: `autostop_manager/mcp_tools.py`
- Test: `tests/test_memory_curator.py`

- [ ] **Step 1: Write failing tests**

```python
from autostop_manager.memory_curator import audit_memory, curate_memory
from autostop_manager.storage import ManagerMemoryStore


def test_recall_ranks_tags_importance_and_priority_above_plain_recency(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    store.remember("Generic unrelated card cleanup note", title="old", tags=["misc"])
    important = store.remember(
        "During board cleanup, never move cards between columns.",
        title="board cleanup no movement",
        tags=["board-cleanup", "прибейсь"],
        importance=0.95,
    )

    result = store.recall("прибейсь карточки", limit=5)

    assert result["items"][0]["id"] == important["id"]
    assert result["items"][0]["kind"] == "note"
    assert result["items"][0]["score"] > 0
    assert result["items"][0]["last_used_at"]


def test_memory_audit_finds_duplicates_expired_and_superseded(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    first = store.remember("Supplier passwords must never be stored.", kind="fact", tags=["security"])
    duplicate = store.remember("Supplier passwords must never be stored.", kind="fact", tags=["security"])
    expired = store.remember(
        "Temporary supplier quote expires tomorrow.",
        kind="fact",
        tags=["quote"],
        expires_at="2000-01-01T00:00:00+00:00",
    )
    superseded = store.remember(
        "Old cleanup command may move cards.",
        title="old cleanup rule",
        tags=["board-cleanup"],
        supersedes_id=first["id"],
    )

    result = audit_memory(store)

    assert result["ok"] is True
    assert any(item["ids"] == [first["id"], duplicate["id"]] for item in result["duplicates"])
    assert any(item["id"] == expired["id"] for item in result["expired"])
    assert any(item["id"] == superseded["id"] for item in result["superseded"])


def test_curate_memory_can_mark_duplicates_archived_without_deleting(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    store.remember("Duplicate operational note", tags=["ops"])
    second = store.remember("Duplicate operational note", tags=["ops"])

    result = curate_memory(store, apply=True)
    recalled = store.recall("Duplicate operational note", limit=5)

    assert result["archived_duplicates"] == [second["id"]]
    assert all(item["id"] != second["id"] for item in recalled["items"])
```

- [ ] **Step 2: Run tests and confirm they fail because curator support is missing**

Run: `python -m pytest tests/test_memory_curator.py -q`

Expected: FAIL with missing module/signatures or unsupported remember metadata.

- [ ] **Step 3: Extend memory storage**

Allow `remember()` to accept `importance`, `expires_at`, `supersedes_id`, `sensitivity`, and `confidence`. Add `archived_at` to notes/facts and create FTS5 tables when SQLite supports FTS5.

- [ ] **Step 4: Improve recall ranking**

Rank text matches with FTS5 when available, fall back to token scoring, include metadata boosts, exclude archived/expired/superseded records, and update `last_used_at`.

- [ ] **Step 5: Add memory audit and curation**

Implement duplicate detection, expired/superseded lists, and non-destructive duplicate archiving.

- [ ] **Step 6: Verify focused tests pass**

Run: `python -m pytest tests/test_memory_curator.py tests/test_storage.py -q`

Expected: PASS.

### Task 3: CLI, MCP, Docs, and Full Verification

**Files:**
- Modify: `README.md`
- Modify: `docs/agent/autostop_manager_skill.md`
- Modify: `docs/agent/knowledge_base_index.md`
- Modify: `docs/agent/knowledge_shelves.md`
- Modify: `docs/agent/manager_mcp_catalog.json`
- Modify: `docs/agent/manager_rules.json`
- Modify: `docs/agent/operating_playbook.json`
- Test: `tests/test_cli.py`
- Test: `tests/test_mcp_tools.py`

- [ ] **Step 1: Add CLI/MCP tests for new commands and tools**

Add tests that `annotations-audit`, `memory-audit`, and `memory-curate` are callable through the CLI/MCP surfaces.

- [ ] **Step 2: Implement CLI/MCP wrappers**

Expose `audit_knowledge_annotations`, `audit_memory`, and `curate_memory`.

- [ ] **Step 3: Update durable docs and seed rules**

Document the new startup rule: use compact annotations before broad section reads; run memory audit before major memory cleanup.

- [ ] **Step 4: Run sync/audit/smoke/full tests**

Run:

```powershell
python -m json.tool docs\agent\manager_rules.json > $null
python -m json.tool docs\agent\manager_mcp_catalog.json > $null
python -m json.tool docs\agent\operating_playbook.json > $null
python -m autostop_manager.cli seed-rules
python -m autostop_manager.cli knowledge-sync
python -m autostop_manager.cli knowledge-audit
python -m autostop_manager.cli skills-audit
python -m autostop_manager.cli memory-audit
python -m pytest -q
```

Expected: all commands exit 0 and pytest reports all tests passing.
