# Manager Feedback Memory Navigation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add feedback-based lessons and memory navigation tools to AutostopManager without automating CRM work or turning manager output into rigid templates.

**Architecture:** Extend the existing SQLite-backed `ManagerMemoryStore` with a focused `lessons` table and query helpers. Expose the new behavior through the same CLI and MCP registration layer used by `remember`, `recall`, and `today_context`, then update tests and agent docs/catalogs.

**Tech Stack:** Python 3.11, SQLite, argparse CLI, FastMCP-compatible tool registration, pytest.

---

## File Map

- Modify `autostop_manager/storage.py`: add `lessons` table, lesson creation/search, memory map/topics/context/gaps helpers.
- Modify `autostop_manager/cli.py`: add `learn`, `lessons`, `memory-map`, `memory-topics`, `memory-context`, and `memory-gaps` commands.
- Modify `autostop_manager/mcp_tools.py`: expose `learn_from_feedback`, `recall_lessons`, `memory_map`, `memory_topics`, `memory_context_for`, and `memory_gaps`.
- Modify `tests/test_storage.py`: cover storage behavior for lessons and navigation.
- Modify `tests/test_cli.py`: cover new parser commands.
- Modify `tests/test_mcp_tools.py`: cover new MCP tool registration and behavior.
- Modify `docs/agent/manager_mcp_catalog.json`: update tool count, lists, contracts, and memory guidance.
- Modify `docs/agent/autostop_manager_skill.md`: instruct manager to use `memory_context_for` before contextual work and `learn_from_feedback` after strong feedback signals.
- Modify `docs/agent/manager_rules.json`: add concise rules for feedback learning and memory navigation.
- Modify `README.md`: document the new CLI examples.

## Task 1: Storage Tests For Lessons

**Files:**
- Modify: `tests/test_storage.py`
- Modify after red: `autostop_manager/storage.py`

- [ ] **Step 1: Write failing storage tests**

Add tests that describe the desired API:

```python
def test_learn_from_feedback_creates_searchable_lesson(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")

    lesson = store.learn_from_feedback(
        "В карточках писать живо и коротко",
        applies_to="crm_cleanup",
        signal="owner_correction",
        recommendation="Писать одну человеческую строку со статусом и следующим шагом.",
        avoid="Не писать длинный сухой AI-шаблон.",
        importance=0.9,
        confidence=1.0,
        tags=["карточки", "стиль"],
    )

    assert lesson["kind"] == "lesson"
    assert lesson["applies_to"] == "crm_cleanup"
    assert lesson["confidence"] == 1.0
    assert lesson["importance"] == 0.9

    result = store.recall_lessons("живую строку", applies_to="crm_cleanup", tags=["стиль"])
    assert result["total_matches"] == 1
    assert result["items"][0]["id"] == lesson["id"]
    assert result["items"][0]["score"] > 0
    assert "recommendation" in result["items"][0]["matched_fields"]
```

- [ ] **Step 2: Verify red**

Run:

```powershell
python -m pytest tests/test_storage.py::test_learn_from_feedback_creates_searchable_lesson -q
```

Expected: fail because `ManagerMemoryStore.learn_from_feedback` does not exist.

- [ ] **Step 3: Implement minimal lesson storage**

Add a `lessons` table to `initialize`, add `learn_from_feedback`, include `lesson` in row shaping and search fields, and add `recall_lessons`.

- [ ] **Step 4: Verify green**

Run:

```powershell
python -m pytest tests/test_storage.py::test_learn_from_feedback_creates_searchable_lesson -q
```

Expected: pass.

## Task 2: Storage Tests For Memory Navigation

**Files:**
- Modify: `tests/test_storage.py`
- Modify after red: `autostop_manager/storage.py`

- [ ] **Step 1: Write failing navigation tests**

Add tests:

```python
def test_memory_navigation_map_topics_context_and_gaps(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    store.remember("Владелец любит живой тон в карточках", kind="fact", category="style", tags=["карточки"])
    store.add_task("Проверить счет на оплату", tags=["счета"])
    store.learn_from_feedback(
        "Не писать роботизированные заметки",
        applies_to="crm_cleanup",
        signal="owner_correction",
        recommendation="Писать как помощник управляющего.",
        avoid="Не использовать сухой шаблон.",
        tags=["карточки", "стиль"],
    )

    memory_map = store.memory_map()
    assert memory_map["sections"]["lessons"]["count"] == 1
    assert memory_map["sections"]["facts"]["count"] == 1
    assert "memory_context_for" in memory_map["recommended_flow"]

    topics = store.memory_topics()
    assert topics["categories"]["style"]["count"] >= 1
    assert topics["tags"]["карточки"]["count"] >= 2

    context = store.memory_context_for("уборка crm карточек живым языком")
    assert context["query"] == "уборка crm карточек живым языком"
    assert context["lessons"]
    assert context["preferences_or_facts"]
    assert context["source_boundaries"]

    gaps = store.memory_gaps()
    assert gaps["empty_sections"]["reminders"] >= 0
    assert "conflicts" in gaps
```

- [ ] **Step 2: Verify red**

Run:

```powershell
python -m pytest tests/test_storage.py::test_memory_navigation_map_topics_context_and_gaps -q
```

Expected: fail because navigation methods do not exist.

- [ ] **Step 3: Implement navigation helpers**

Implement `memory_map`, `memory_topics`, `memory_context_for`, and `memory_gaps` using existing tables and the new lessons table. Keep outputs compact and deterministic.

- [ ] **Step 4: Verify green**

Run:

```powershell
python -m pytest tests/test_storage.py::test_memory_navigation_map_topics_context_and_gaps -q
```

Expected: pass.

## Task 3: CLI Coverage And Commands

**Files:**
- Modify: `tests/test_cli.py`
- Modify after red: `autostop_manager/cli.py`

- [ ] **Step 1: Write failing parser tests**

Add parser assertions:

```python
def test_learning_and_navigation_commands_parse():
    parser = build_parser()

    args = parser.parse_args([
        "learn",
        "Писать живее",
        "--applies-to",
        "crm_cleanup",
        "--signal",
        "owner_correction",
        "--recommendation",
        "Одна короткая строка",
        "--avoid",
        "Длинный шаблон",
        "--importance",
        "0.8",
        "--confidence",
        "1.0",
        "--tags",
        "карточки,стиль",
    ])
    assert args.command == "learn"
    assert args.applies_to == "crm_cleanup"
    assert args.importance == 0.8

    assert parser.parse_args(["lessons", "карточки"]).command == "lessons"
    assert parser.parse_args(["memory-map"]).command == "memory-map"
    assert parser.parse_args(["memory-topics"]).command == "memory-topics"
    assert parser.parse_args(["memory-context", "crm карточки"]).command == "memory-context"
    assert parser.parse_args(["memory-gaps"]).command == "memory-gaps"
```

- [ ] **Step 2: Verify red**

Run:

```powershell
python -m pytest tests/test_cli.py::test_learning_and_navigation_commands_parse -q
```

Expected: fail because the commands do not exist.

- [ ] **Step 3: Implement CLI commands**

Add commands and route them to store methods. Use `_print_json` and the existing `_tags` helper.

- [ ] **Step 4: Verify green**

Run:

```powershell
python -m pytest tests/test_cli.py::test_learning_and_navigation_commands_parse -q
```

Expected: pass.

## Task 4: MCP Coverage And Tools

**Files:**
- Modify: `tests/test_mcp_tools.py`
- Modify after red: `autostop_manager/mcp_tools.py`

- [ ] **Step 1: Write failing MCP test**

Add:

```python
def test_learning_and_navigation_tools_are_registered(tmp_path):
    server = DummyServer()
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    register_manager_memory_tools(server, store)

    for name in [
        "learn_from_feedback",
        "recall_lessons",
        "memory_map",
        "memory_topics",
        "memory_context_for",
        "memory_gaps",
    ]:
        assert name in server.tools

    lesson = server.tools["learn_from_feedback"](
        "Писать карточки живее",
        applies_to="crm_cleanup",
        signal="owner_praise",
        recommendation="Оставлять короткий человеческий следующий шаг.",
        avoid="Не писать длинный шаблон.",
        tags=["карточки"],
    )
    assert lesson["kind"] == "lesson"

    assert server.tools["recall_lessons"]("человеческий", applies_to="crm_cleanup")["items"]
    assert server.tools["memory_map"]()["sections"]["lessons"]["count"] == 1
    assert server.tools["memory_topics"]()["tags"]["карточки"]["count"] == 1
    assert server.tools["memory_context_for"]("crm карточки")["lessons"]
    assert "empty_sections" in server.tools["memory_gaps"]()
```

- [ ] **Step 2: Verify red**

Run:

```powershell
python -m pytest tests/test_mcp_tools.py::test_learning_and_navigation_tools_are_registered -q
```

Expected: fail because the tools are not registered.

- [ ] **Step 3: Implement MCP tools**

Register the six tools with concise descriptions and pass through optional filters.

- [ ] **Step 4: Verify green**

Run:

```powershell
python -m pytest tests/test_mcp_tools.py::test_learning_and_navigation_tools_are_registered -q
```

Expected: pass.

## Task 5: Docs And Catalogs

**Files:**
- Modify: `README.md`
- Modify: `docs/agent/manager_mcp_catalog.json`
- Modify: `docs/agent/autostop_manager_skill.md`
- Modify: `docs/agent/manager_rules.json`

- [ ] **Step 1: Update documentation**

Document the new tools, add examples, and add rules that tell the manager to learn from strong signals and use memory context before contextual work.

- [ ] **Step 2: Validate JSON**

Run:

```powershell
python -m json.tool docs\agent\manager_mcp_catalog.json > $null
python -m json.tool docs\agent\manager_rules.json > $null
```

Expected: no output and exit code 0.

## Task 6: Full Verification

**Files:**
- Read-only verification across repository.

- [ ] **Step 1: Run focused tests**

Run:

```powershell
python -m pytest tests/test_storage.py tests/test_cli.py tests/test_mcp_tools.py -q
```

Expected: all focused tests pass.

- [ ] **Step 2: Run full tests**

Run:

```powershell
python -m pytest -q
```

Expected: all tests pass.

- [ ] **Step 3: Sync and audit knowledge**

Run:

```powershell
python -m autostop_manager.cli knowledge-sync
python -m autostop_manager.cli knowledge-audit
```

Expected: both return `ok=true` and no missing files.

- [ ] **Step 4: Diff hygiene**

Run:

```powershell
git diff --check
git status --short
```

Expected: no whitespace errors. Existing untracked `output/` may remain untouched.
