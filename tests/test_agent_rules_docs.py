from __future__ import annotations

import csv
import json
from pathlib import Path
import re
import subprocess
import tomllib

ROOT = Path(__file__).resolve().parents[1]


def _items(value: str) -> list[str]:
    return [item.strip() for item in value.split("|") if item.strip()]


def _assert_contains(text: str, expected: str) -> None:
    for item in _items(expected):
        assert item in text


def _text(*parts: str) -> str:
    return ROOT.joinpath(*parts).read_text(encoding="utf-8")


def _payload(*parts: str) -> dict:
    return json.loads(_text(*parts))


def _tracked(*paths: str) -> list[str]:
    return subprocess.run(
        ["git", "ls-files", *paths], cwd=ROOT, check=True, capture_output=True, text=True, encoding="utf-8"
    ).stdout.splitlines()


def test_codex_native_startup_files_are_present_and_safe():
    agents_path = ROOT / "AGENTS.md"
    config_path = ROOT / ".codex" / "config.toml"
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert not (ROOT / "agent.md").exists()
    assert agents_path.is_file()
    assert config_path.is_file()

    agents = agents_path.read_text(encoding="utf-8")
    assert len(agents.encode("utf-8")) < 32 * 1024
    _assert_contains(
        agents,
        "Compact startup contract|agent-brief|knowledge-probe|agent_bootstrap|agent_board_digest|Gateway v2 ledger|AutoStop App owns Store|Manager keeps routes|dry-run|idempotently|persistent Manager database|$manage-autostop-store",
    )
    assert "Store work is paused" not in agents
    assert "Store Scope" not in agents
    assert "explicit owner Store tasks" not in agents

    config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert config["project_doc_max_bytes"] == 65536
    assert config["project_doc_fallback_filenames"] == ["AGENTS.md", "README.md"]
    assert config["mcp_servers"]["autostopcrm"]["url"] == "https://crm.autostopcrm.ru/mcp"
    assert config["mcp_servers"]["autostopcrm"]["enabled"] is True
    assert config["mcp_servers"]["autostopcrm"]["tool_timeout_sec"] == 90
    diagnostics = config["mcp_servers"]["autostop_remote_diagnostics"]
    assert diagnostics["command"] == "/usr/local/libexec/autostop-remote-staging-mcp"
    assert diagnostics["enabled"] is True
    assert diagnostics["required"] is False
    assert diagnostics["default_tools_approval_mode"] == "prompt"
    assert diagnostics["enabled_tools"] == _items("device_status|observe|activate_node|tap|swipe|back|stop_session")
    assert {"open_launch", "set_text"}.isdisjoint(diagnostics["enabled_tools"])
    assert {"env", "env_vars"}.isdisjoint(diagnostics)
    assert "tools" not in diagnostics
    assert set(config["mcp_servers"]) == {"autostopcrm", "autostop_remote_diagnostics"}
    assert {"apps", "connectors", "plugins"}.isdisjoint(config)

    forbidden_config_keys = set(
        _items(
            "approval_policy|sandbox_mode|model|model_provider|model_providers|openai_base_url|chatgpt_base_url|otel|auth|profiles|profile"
        )
    )
    assert forbidden_config_keys.isdisjoint(config)
    config_text = config_path.read_text(encoding="utf-8").casefold()
    assert not re.search(r"(api[_-]?key|token|secret|password|credential)", config_text)

    assert "`AGENTS.md` — canonical startup contract." in readme
    assert "agent.md" not in readme
    assert not (ROOT / "docs" / "agent" / "voice_agent_brief.md").exists()


def test_home_pc_remote_access_is_documented_as_current_capability():
    access_doc = _text("docs", "agent", "codex_home_pc_reverse_ssh.md")
    _assert_contains(
        access_doc,
        "home-pc|DESKTOP-BUSO4I8|127.0.0.1:22220|codex-home-tunnel|codexadmin|\\Autostop\\CodexRemoteReverseTunnel|BatchMode=yes|host-key mismatch|ssh -G <alias>|autostop-vps27560|autostop-vps27560-alt|/root/.codex/CODEX_VPN_FST_ACCESS.md|/opt/autostop-managed-pc/README.md",
    )
    assert len(access_doc.splitlines()) <= 70

    bootstrap = (ROOT / "scripts" / "codex_home_pc_bootstrap.ps1").read_text(encoding="utf-8")
    assert '[string]$ServerHost = "46.8.254.189"' in bootstrap
    assert "46.8.254.243" not in bootstrap

    route = _payload("docs", "agent", "knowledge_map.json")
    assert "remote_codex_access" in route["domains"]
    assert "docs/agent/codex_home_pc_reverse_ssh.md" in route["domains"]["remote_codex_access"]["primary_files"]


def test_pad_vii_playbook_requires_the_complete_current_status_gate():
    playbook = (ROOT / "docs" / "agent" / "remote_diagnostics_pad_vii_playbook.md").read_text(encoding="utf-8")

    _assert_contains(
        playbook,
        "connected=true|ready=true|mode=CONTROL|controlEnabled=true|screenState=onUnlocked|mediaProjectionActive=true|accessibilityEnabled=true|foregroundKind=launch|commandAvailable=true",
    )


def test_documentation_hygiene_keeps_docs_compact_and_single_owned():
    checked_paths = [
        ROOT / "AGENTS.md",
        ROOT / "README.md",
    ]
    combined = "\n".join(path.read_text(encoding="utf-8") for path in checked_paths)

    _assert_contains(combined, "Compact startup contract|Keep one owner for each rule|docs/agent/deployment_runbook.md")

    knowledge_map = _payload("docs", "agent", "knowledge_map.json")
    allowed = {"title", "primary_files", "reference_files", "optional_runtime_files", "skill_path"}
    assert knowledge_map["format"] == "knowledge_navigation_v1"
    assert all(set(domain) <= allowed for domain in knowledge_map["domains"].values())
    assert not (ROOT / "docs" / "agent" / "knowledge_annotations.jsonl").exists()
    startup_files = knowledge_map["domains"]["startup_and_identity"]["primary_files"]
    assert ".codex/config.toml" in startup_files
    assert "docs/agent/crm_mcp_catalog.json" in startup_files
    assert "docs/agent/voice_agent_brief.md" not in startup_files
    assert "reference_files" not in knowledge_map["domains"]["startup_and_identity"]
    store = knowledge_map["domains"]["store_management"]
    assert store["skill_path"] == ".agents/skills/manage-autostop-store/SKILL.md"
    assert store["primary_files"][0] == ".agents/skills/manage-autostop-store/SKILL.md"
    telegram = knowledge_map["domains"]["telegram_operations"]
    assert telegram["skill_path"] == ".agents/skills/manage-owner-telegram/SKILL.md"


def test_routing_instruction_footprint_stays_below_release_ceiling():
    paths = [
        ROOT / "AGENTS.md",
        ROOT / "docs/agent/command_routes.json",
        ROOT / "docs/agent/knowledge_map.json",
        ROOT / "docs/agent/knowledge_annotations.jsonl",
        ROOT / "docs/agent/manager_rules.json",
        ROOT / "docs/agent/manager_mcp_catalog.json",
        ROOT / "docs/agent/crm_mcp_catalog.json",
    ]
    sizes = {path.name: path.stat().st_size if path.exists() else 0 for path in paths}

    assert sum(sizes.values()) <= 70_000
    assert sizes["AGENTS.md"] <= 6_500
    assert sizes["knowledge_map.json"] <= 19_000
    assert sizes["manager_mcp_catalog.json"] + sizes["crm_mcp_catalog.json"] <= 5_000
    assert sizes["manager_rules.json"] <= 4_500
    routing_index_paths = [
        "autostop_manager/knowledge_base.py",
        "autostop_manager/context.py",
        "tests/test_knowledge_base.py",
        "tests/test_knowledge_router_v2.py",
        "tests/test_agent_rules_docs.py",
    ]
    assert sum(len((ROOT / path).read_text(encoding="utf-8").splitlines()) for path in routing_index_paths) <= 3_600


def test_manager_rules_only_hold_cross_system_runtime_invariants():
    payload = _payload("docs", "agent", "manager_rules.json")

    assert payload["format"] == "manager_runtime_invariants_v2"
    assert {rule["id"] for rule in payload["rules"]} == set(
        _items(
            "source-and-privacy|route-and-authority|guarded-mutation|store-client-flow|sensitive-actions|workflow-and-release"
        )
    )
    assert all(set(rule) == {"id", "priority", "rule"} for rule in payload["rules"])
    assert [rule["priority"] for rule in payload["rules"]] == sorted(rule["priority"] for rule in payload["rules"])
    quote = next(rule["rule"] for rule in payload["rules"] if rule["id"] == "store-client-flow")
    _assert_contains(quote, "Admin V2 conductor|Preliminary clarification|published readback|WAITING_FOR_PAYMENT")


def test_redundant_navigation_and_generated_source_maps_stay_removed():
    removed = _items(
        "docs/agent/knowledge_base_index.md|docs/agent/knowledge_intake_playbook.md|docs/agent/ai_parts_krasnoyarsk_playbook.md|docs/agent/zzap_search_playbook.md|docs/agent/manager_identity.json|docs/agent/memory_policy.json|docs/agent/phone_flow.json|docs/agent/voice_agent_brief.md|.agents/skills/resolve-autostop-service-case/references/evidence-contract.md|docs/agent/automotive_sources/source_cache/ai_parts_krasnoyarsk_project_pack|docs/agent/automotive_sources/brand_source_map.json|docs/agent/automotive_sources/data_type_source_map.json|docs/agent/automotive_sources/dsg_transmission_sources.json|docs/agent/automotive_sources/model_source_overrides.json|docs/agent/automotive_sources/source_cache/offline_parts_catalogs_knowledge_pack/MANIFEST.md|docs/agent/automotive_sources/source_cache/offline_parts_catalogs_knowledge_pack/sources/offline_parts_catalog_sources.json|docs/agent/gmail_mcp_catalog.json|docs/agent/knowledge_annotations.jsonl"
    )

    assert all(not (ROOT / path).exists() for path in removed)


def test_command_registry_v3_contains_only_operational_routing_metadata():
    payload = _payload("docs", "agent", "command_routes.json")

    assert payload["format"] == "agent_command_registry_v3"
    command_ids = [route["command_id"] for route in payload["routes"]]
    assert len(command_ids) == len(set(command_ids))
    allowed = set(_items("command_id|workflow_id|intent|priority|phase|knowledge_domains|effects|dependencies|signals"))
    forbidden = set(
        _items(
            "open_first|memory_queries|required_reads|write_domains|external_connectors|completion_checks|next_actions|read_entity_selection|operation_selection|aliases|keywords"
        )
    )
    for route in payload["routes"]:
        assert set(route) <= allowed
        assert forbidden.isdisjoint(route)
        assert isinstance(route["knowledge_domains"], list)
        assert isinstance(route["effects"], list)
        assert isinstance(route["dependencies"], list)
        assert set(route["signals"]) <= {"phrases", "all", "any", "exclude", "action"}

    integration_route = next(
        route for route in payload["routes"] if route["workflow_id"] == "crm_agent_integration_audit"
    )
    assert "apps" not in integration_route["signals"]["any"]


def test_mcp_catalogs_are_minimal_verified_surface_manifests():
    manager_catalog = _payload("docs", "agent", "manager_mcp_catalog.json")
    crm_catalog = _payload("docs", "agent", "crm_mcp_catalog.json")
    expected_keys = set(_items("format|source|expected_tool_count|expected_tool_names|schema_fingerprint|verified_at"))
    for catalog in (manager_catalog, crm_catalog):
        assert set(catalog) == expected_keys
        assert catalog["format"] == "mcp_surface_manifest_v1"
        names = catalog["expected_tool_names"]
        assert names == sorted(set(names))
        assert catalog["expected_tool_count"] == len(names)
        assert re.fullmatch(r"[0-9a-f]{64}", catalog["schema_fingerprint"])
        assert "sha256(canonical sorted [{name,inputSchema}])" in catalog["source"]

    assert {
        "audit_knowledge_annotations",
        "crm_health_plan",
        "memory_review",
        "prepare_crm_card_action",
        "recommend_service_management_actions",
    }.isdisjoint(manager_catalog["expected_tool_names"])
    assert crm_catalog["expected_tool_count"] == 24
    assert "agent_finance_workflow" in crm_catalog["expected_tool_names"]


def test_store_procedures_live_in_playbook_not_route_or_catalog():
    routes_payload = _payload("docs", "agent", "command_routes.json")
    manager_catalog = _text("docs", "agent", "manager_mcp_catalog.json")
    playbook = _text("docs", "agent", "store_management_playbook.md")
    routes = {route["command_id"]: route for route in routes_payload["routes"]}

    assert set(
        _items(
            "store_read_workflow|store_quote_draft|store_product_create|store_price_management|store_order_ready|store_management_workflow|store_quote_estimate_publish|store_customer_response_publish"
        )
    ) <= set(routes)
    assert "store_management" in routes["store_management_workflow"]["knowledge_domains"]
    assert routes["store_management_workflow"]["effects"] == ["store_write"]
    assert routes["store_quote_draft"]["workflow_id"] == "store_quote_conductor"
    assert routes["store_quote_draft"]["effects"] == ["store_write"]
    assert routes["store_product_create"]["effects"] == ["store_write", "finance", "destructive"]
    assert routes["store_price_management"]["effects"] == ["store_write", "finance", "destructive"]
    assert routes["store_order_ready"]["effects"] == ["store_write", "external_send", "destructive"]
    assert routes["store_quote_estimate_publish"]["workflow_id"] == "store_quote_conductor"
    assert routes["store_quote_estimate_publish"]["effects"] == _items("store_write|external_send")
    store_phrases = routes["store_quote_draft"]["signals"]["phrases"]
    assert "подготовь черновик ответа клиенту по заявке магазина" in store_phrases
    assert "ответь клиенту по заявке магазина" not in store_phrases
    response_route = routes["store_customer_response_publish"]
    assert response_route["workflow_id"] == "store_quote_conductor"
    assert response_route["effects"] == ["store_write", "external_send"]
    assert response_route["knowledge_domains"] == _items(
        "store_management|vehicle_identity_and_oem|parts_sourcing|telegram_operations"
    )
    assert "обработай новую заявку магазина" in response_route["signals"]["phrases"]
    assert "переведи заявку в ждёт согласования" in response_route["signals"]["phrases"]
    assert "planned_changes_by_action" not in manager_catalog
    normalized_playbook = " ".join(playbook.split())
    _assert_contains(
        normalized_playbook,
        "AutoStop App is authoritative|current named Gateway tools|Drom/Avito|store_quote_conductor|Admin V2 `estimate_draft`|preliminary orientation|typed Telegram adapter|WAITING_FOR_PAYMENT|exact reread -> action contract -> dry-run -> apply -> exact readback",
    )
    assert "replace_quote_offer_drafts" not in playbook

    workflow = (ROOT / ".github" / "workflows" / "quality.yml").read_text(encoding="utf-8")
    _assert_contains(
        workflow,
        'python-version: "3.11"|pip install -e ".[dev]"|knowledge-sync|knowledge-audit|skills-audit|cleanup-audit|ruff check .|ruff format --check autostop_manager tests|mypy autostop_manager|pytest -q|coverage report --fail-under=82|workflow_dispatch|pull_request',
    )


def test_work_telegram_is_linked_to_one_exact_store_client_dialogue():
    skill = _text(".agents", "skills", "manage-owner-telegram", "SKILL.md")
    playbook = _text("docs", "agent", "telegram_workflow_playbook.md")
    normalized_skill = " ".join(skill.split())
    _assert_contains(normalized_skill, "`personal` или `work`|private peer|dry-run/apply|readback|quote conductor")
    _assert_contains(
        playbook,
        "owner-selected `personal` or `work` bridge|one exact private peer|dry-run|independently reread|Store Client Dialogue|published estimate|WAITING_FOR_PAYMENT",
    )


def test_release_runbook_isolates_knowledge_preflight_from_persistent_db():
    runbook = (ROOT / "docs" / "agent" / "deployment_runbook.md").read_text(encoding="utf-8")

    assert "mktemp -d /tmp/autostop-manager-release-gates.XXXXXX" in runbook
    assert 'export AUTOSTOP_MANAGER_DB="$release_gate_tmp/preflight.sqlite3"' in runbook
    assert "trap cleanup_release_gate_tmp EXIT" in runbook
    assert runbook.index("AUTOSTOP_MANAGER_DB") < runbook.index("knowledge-sync")


def test_board_cleanup_docs_do_not_reintroduce_old_archive_or_description_preview_policy():
    checked_paths = [
        ROOT / "docs" / "agent" / "board_cleanup_autopilot_playbook.md",
        ROOT / "docs" / "agent" / "command_routes.json",
        ROOT / "docs" / "agent" / "manager_mcp_catalog.json",
        ROOT / "docs" / "agent" / "manager_rules.json",
    ]
    stale_fragments = [
        "archive completed cards when safe",
        "archive completed cards only when safe",
        "safe archive",
        "first five visible lines",
        "tag/mark/archive",
        "detailed descriptions",
        "detailed recoverable card text",
        "oem/price/source conclusion",
        "source conclusions into the card",
        "<u>",
        "</u>",
        "rare underline",
        "if the crm renderer supports it",
        "при" + "бейсь",
        "пере" + "берись",
    ]

    combined = "\n".join(path.read_text(encoding="utf-8").casefold() for path in checked_paths)

    for fragment in stale_fragments:
        assert fragment not in combined
    assert "board_summary" in combined
    assert "separate explicit owner command" in combined


def test_board_cleanup_description_and_structured_field_contract_is_documented():
    playbook = (ROOT / "docs" / "agent" / "board_cleanup_autopilot_playbook.md").read_text(encoding="utf-8")
    description_standard = (ROOT / "docs" / "agent" / "crm_card_description_standard.md").read_text(encoding="utf-8")
    route = json.loads((ROOT / "docs" / "agent" / "command_routes.json").read_text(encoding="utf-8"))

    assert "This playbook is the only detailed source of truth" in playbook
    assert "crm_card_description_standard.md" in playbook
    _assert_contains(
        description_standard,
        "complete, coherent and gradually developing story|There are no mandatory headings, blocks, dates, line counts|one or two natural sentences|reread the entire existing description|what was found, what was agreed, what was done|fresh natural-language|complaint, findings and diagnostic results|Do not merely append the latest event|Facts To Preserve And Exclude",
    )
    _assert_contains(
        playbook, "phone goes to the client|VIN/plate/mileage|vehicle` as a compact make/model|no more than three tags"
    )
    assert "repair_orders_changed=0 and payments_changed=0" in playbook

    cleanup_route = next(item for item in route["routes"] if item["command_id"] == "board_cleanup_autopilot")
    assert {"board_cleanup_autopilot", "crm_card_description_standard"} <= set(cleanup_route["knowledge_domains"])
    assert "crm_write" in cleanup_route["effects"]


def test_business_documents_route_requires_crm_print_module_for_autostop_documents():
    playbook = (ROOT / "docs" / "agent" / "business_document_quality_playbook.md").read_text(encoding="utf-8")
    routes = json.loads((ROOT / "docs" / "agent" / "command_routes.json").read_text(encoding="utf-8"))

    _assert_contains(
        playbook,
        "CRM print module|create_document_without_card_pdf|download_repair_order_print_pdf|standard AutoStop template|tax_label|Без НДС|Do not build independent PDF/HTML templates|Документ без карточки|infer the standard document type",
    )

    route = next(item for item in routes["routes"] if item["command_id"] == "business_document_workflow")
    assert "business_documents" in route["knowledge_domains"]
    assert "document" in route["effects"]


def test_removed_second_brain_terms_are_not_tracked_anymore():
    blocked_terms = [
        "Ob" + "sidian",
        "обси" + "диан",
        "va" + "ult",
        "Google " + "Drive",
        "Мой " + "диск" + "\\Ob" + "sidian CRM",
        "setup_autostop_" + "ob" + "sidian_" + "va" + "ult",
    ]
    tracked = subprocess.run(
        ["git", "ls-files"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.splitlines()
    offenders: list[str] = []
    for raw_path in tracked:
        path = ROOT / raw_path
        if not path.is_file():
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            content = path.read_text(encoding="utf-8", errors="ignore")
        lowered = content.casefold()
        for term in blocked_terms:
            if term.casefold() in lowered:
                offenders.append(raw_path)
                break

    assert offenders == []


def test_tracked_docs_do_not_reference_old_windows_profile_paths():
    old_profile = "986" + "0606"
    generic_profile = "Us" + "er"
    blocked_fragments = [
        "C:/Users/" + generic_profile,
        "C:\\Users\\" + generic_profile,
        "C:/Users/" + old_profile,
        "C:\\Users\\" + old_profile,
    ]
    tracked = subprocess.run(
        ["git", "ls-files"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.splitlines()
    offenders: list[str] = []
    for raw_path in tracked:
        path = ROOT / raw_path
        if not path.is_file():
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            content = path.read_text(encoding="utf-8", errors="ignore")
        if any(fragment in content for fragment in blocked_fragments):
            offenders.append(raw_path)

    assert offenders == []


def test_tracked_markdown_does_not_have_adjacent_duplicate_content_lines():
    offenders: list[str] = []
    for raw_path in subprocess.run(
        ["git", "ls-files", "*.md"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.splitlines():
        path = ROOT / raw_path
        if not path.is_file():
            continue
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        for index in range(1, len(lines)):
            previous = lines[index - 1].strip()
            current = lines[index].strip()
            if previous and previous == current and len(current) > 20:
                offenders.append(f"{raw_path}:{index + 1}")

    assert offenders == []


def test_every_tracked_agent_data_line_is_structurally_readable():
    tracked = subprocess.run(
        ["git", "ls-files", "AGENTS.md", "README.md", "docs/agent"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.splitlines()
    failures: list[str] = []

    for raw_path in tracked:
        path = ROOT / raw_path
        if not path.is_file():
            continue
        try:
            if path.suffix == ".json":
                json.loads(path.read_text(encoding="utf-8-sig"))
            elif path.suffix == ".jsonl":
                for _line_number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
                    if line.strip():
                        json.loads(line)
            elif path.suffix == ".csv":
                rows = list(csv.reader(path.read_text(encoding="utf-8-sig").splitlines()))
                if not rows or any(len(row) != len(rows[0]) for row in rows):
                    failures.append(f"{raw_path}:inconsistent_csv_width")
            elif path.suffix == ".md":
                fence_count = sum(
                    line.lstrip().startswith("```") for line in path.read_text(encoding="utf-8").splitlines()
                )
                if fence_count % 2:
                    failures.append(f"{raw_path}:unbalanced_code_fence")
        except (csv.Error, json.JSONDecodeError, UnicodeError) as exc:
            failures.append(f"{raw_path}:{exc}")

    assert failures == []


def test_every_tracked_agent_document_has_a_knowledge_map_route():
    knowledge_map = json.loads((ROOT / "docs" / "agent" / "knowledge_map.json").read_text(encoding="utf-8"))
    routed: set[str] = set()
    for domain in knowledge_map["domains"].values():
        for field in ("primary_files", "reference_files", "optional_runtime_files"):
            routed.update(domain.get(field, []))

    tracked = subprocess.run(
        ["git", "ls-files", "AGENTS.md", "README.md", "docs/agent"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.splitlines()
    existing = {raw_path for raw_path in tracked if (ROOT / raw_path).is_file()}

    assert existing <= routed


def test_agent_docs_do_not_expose_partsapi_test_keys_or_crm_contacts():
    tracked_docs = subprocess.run(
        ["git", "ls-files", "docs/agent"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.splitlines()
    offenders: list[str] = []
    blocked_patterns = [
        re.compile(r"Тестовый ключ", re.IGNORECASE),
        re.compile(r"key=[0-9a-f]{30,}", re.IGNORECASE),
        re.compile(r"PARTSAPI_KEY=(?!<|\$|your|YOUR|xxx|\*)\S+", re.IGNORECASE),
        re.compile(r"(?<![\d.])(?:\+?7|8)\s?[\d\s() -]{9,18}"),
    ]
    for raw_path in tracked_docs:
        path = ROOT / raw_path
        if not path.is_file():
            continue
        content = path.read_text(encoding="utf-8", errors="ignore")
        if any(pattern.search(content) for pattern in blocked_patterns):
            offenders.append(raw_path)

    assert offenders == []
