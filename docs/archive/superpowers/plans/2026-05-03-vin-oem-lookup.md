# VIN/OEM Lookup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn AutostopManager into a source-aware VIN and chassis-number lookup layer that can classify identifiers, route to the right official catalogs, and return structured OEM lookup guidance with provenance.

**Architecture:** Keep the current memory layer intact, and add a separate lookup contour that handles identifier normalization, source selection, and remote decode requests. The first pass should decode standard VINs through NHTSA vPIC, then route Japanese frame/chassis numbers and brand-specific lookups through documented catalog paths and source hints. Expose the same capability through both CLI and MCP so the assistant can use it interactively and persistently.

**Tech Stack:** Python 3.11, stdlib HTTP (`urllib.request`), SQLite memory store already in repo, MCP FastMCP server, pytest.

---

### Task 1: Add VIN/OEM lookup docs and source registry

**Files:**
- Create: `docs/agent/vin_oem_lookup_playbook.md`
- Create: `docs/agent/vin_oem_sources.json`
- Modify: `README.md`
- Modify: `docs/agent/autostop_manager_skill.md`
- Modify: `docs/agent/vehicle_identity_playbook.md`
- Modify: `docs/agent/parts_search_playbook.md`
- Modify: `docs/agent/operating_playbook.json`
- Modify: `docs/agent/manager_rules.json`

- [ ] **Step 1: Write the failing doc references test by inspection**

Check that every place that currently tells the manager how to handle VINs or parts search now points to the new lookup playbook and source registry, and that the registry includes at least the official NHTSA vPIC route plus the Toyota frame-number path.

- [ ] **Step 2: Run a grep check to confirm missing references**

Run: `rg -n "vin_oem_lookup|vin_oem_sources|vPIC|frame number|OEM" README.md docs/agent`
Expected: the new playbook and registry are referenced from the startup/rules docs, and there are no stale paths left behind.

- [ ] **Step 3: Write the minimal documentation update**

```md
# VIN/OEM Lookup Playbook

1. Classify the identifier first: ISO VIN, Japanese frame/chassis number, or market-specific code.
2. For VINs, decode through NHTSA vPIC first.
3. For Japanese frame numbers, route to the manufacturer catalog or official recall/EPC path that accepts frame number input.
4. Prefer exact OEM, exact cross, then confirmed analog.
5. Store only the durable routing rule and source preference in memory.
```

- [ ] **Step 4: Re-run the grep check**

Run: `rg -n "vin_oem_lookup|vin_oem_sources|vPIC|frame number|OEM" README.md docs/agent`
Expected: all the new references are present and the lookup playbook is linked from the startup and rules documents.

- [ ] **Step 5: Commit the documentation layer**

```bash
git add README.md docs/agent/*
git commit -m "docs: add vin oem lookup protocol"
```

### Task 2: Add identifier normalization and source-routing helpers

**Files:**
- Create: `autostop_manager/vin_lookup.py`
- Modify: `autostop_manager/__init__.py`

- [ ] **Step 1: Write the failing unit tests first**

```python
def test_classify_identifier_detects_vin_and_frame_number():
    assert classify_identifier("JH4DA9350LS000000").kind == "vin"
    assert classify_identifier("GXE10-0088644").kind == "frame_number"


def test_build_lookup_steps_for_toyota_frame_number():
    result = build_lookup_plan("GXE10-0088644")
    assert result.identifier.kind == "frame_number"
    assert any("Toyota" in step.source_name for step in result.steps)
```

- [ ] **Step 2: Run the tests and confirm they fail**

Run: `pytest tests/test_vin_lookup.py -v`
Expected: import or attribute failures before the module exists.

- [ ] **Step 3: Implement the lookup helper module**

```python
def classify_identifier(raw: str) -> IdentifierClassification:
    ...

def normalize_vin(raw: str) -> str:
    ...

def build_lookup_plan(raw: str, model_year: int | None = None) -> LookupPlan:
    ...
```

The implementation should:

1. Strip spaces and punctuation only where safe.
2. Treat 17-character ISO VINs as VINs.
3. Treat Toyota-style and similar market frame numbers as frame numbers instead of forcing VIN decoding.
4. Emit source steps with explicit URLs and notes.

- [ ] **Step 4: Run the tests again**

Run: `pytest tests/test_vin_lookup.py -v`
Expected: the classification and routing tests pass.

- [ ] **Step 5: Commit the lookup helpers**

```bash
git add autostop_manager/__init__.py autostop_manager/vin_lookup.py tests/test_vin_lookup.py
git commit -m "feat: add vin lookup routing helpers"
```

### Task 3: Wire the lookup into CLI and MCP

**Files:**
- Modify: `autostop_manager/cli.py`
- Modify: `autostop_manager/mcp_server.py`
- Modify: `autostop_manager/mcp_tools.py`
- Modify: `autostop_manager/__init__.py`

- [ ] **Step 1: Add parser and tool tests**

```python
def test_cli_parser_has_lookup_command():
    parser = cli.build_parser()
    args = parser.parse_args(["lookup-oem", "JH4DA9350LS000000"])
    assert args.command == "lookup-oem"


def test_lookup_tool_returns_structured_plan():
    result = lookup_original_parts("JH4DA9350LS000000")
    assert result["identifier"]["kind"] == "vin"
    assert result["steps"]
```

- [ ] **Step 2: Confirm the tests fail before wiring**

Run: `pytest tests/test_cli.py tests/test_vin_lookup.py -v`
Expected: parser/tool failures until the CLI and MCP surface the new capability.

- [ ] **Step 3: Implement the CLI and MCP wiring**

```python
@server.tool(
    name="lookup_original_parts",
    description="Classify a VIN or chassis number and return the best OEM lookup route, source hints, and normalized identifier.",
)
def lookup_original_parts(identifier: str, model_year: int | None = None) -> dict[str, Any]:
    ...
```

The CLI should expose the same capability through a `lookup-oem` subcommand and print JSON with the identifier classification, source steps, and warnings.

- [ ] **Step 4: Run the focused tests**

Run: `pytest tests/test_cli.py tests/test_vin_lookup.py -v`
Expected: tests pass and the JSON shape is stable.

- [ ] **Step 5: Commit the wiring changes**

```bash
git add autostop_manager/cli.py autostop_manager/mcp_server.py autostop_manager/mcp_tools.py tests/test_cli.py tests/test_vin_lookup.py
git commit -m "feat: expose vin lookup through cli and mcp"
```

### Task 4: Add remote decode coverage and verification tests

**Files:**
- Modify: `autostop_manager/vin_lookup.py`
- Create: `tests/test_vin_lookup_http.py`
- Modify: `tests/test_storage.py` if shared memory helpers need new rule seeding assertions

- [ ] **Step 1: Write HTTP-mocked decode tests**

```python
def test_vpic_decode_request_uses_model_year_and_json():
    payload = decode_vin_vpic("JH4DA9350LS000000", model_year=1990)
    assert payload["source"] == "NHTSA vPIC"
    assert payload["model_year"] == 1990
```

- [ ] **Step 2: Run the new HTTP test and confirm it fails**

Run: `pytest tests/test_vin_lookup_http.py -v`
Expected: missing network helper or unmocked request failures before implementation.

- [ ] **Step 3: Implement the HTTP request path with stdlib**

```python
def decode_vin_vpic(vin: str, *, model_year: int | None = None) -> dict[str, Any]:
    ...
```

Use a small, explicit timeout and return structured errors instead of raising raw transport exceptions.

- [ ] **Step 4: Run the full test suite**

Run: `pytest -v`
Expected: all tests pass, including the new remote-decode coverage.

- [ ] **Step 5: Commit the verification layer**

```bash
git add autostop_manager/vin_lookup.py tests/test_vin_lookup_http.py tests/test_storage.py
git commit -m "test: cover vin decode and source routing"
```

### Task 5: Final doc sync and runtime sanity check

**Files:**
- Modify: `README.md`
- Modify: `docs/agent/manager_identity.json`
- Modify: `docs/agent/manager_rules.json`

- [ ] **Step 1: Make sure the docs describe the new lookup contour clearly**

The final doc pass should state that:

1. VINs go to vPIC first.
2. Frame/chassis numbers use the market-appropriate catalog path.
3. The assistant returns structured lookup guidance, not unsupported guesses.

- [ ] **Step 2: Run the startup-path smoke check**

Run: `python -m autostop_manager.cli today`
Expected: existing memory behavior still works and the new rules remain compatible with current startup flow.

- [ ] **Step 3: Commit the finished branch**

```bash
git add README.md docs/agent/manager_identity.json docs/agent/manager_rules.json
git commit -m "docs: sync vin lookup operating rules"
```
