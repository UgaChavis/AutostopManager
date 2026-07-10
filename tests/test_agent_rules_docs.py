from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
import tomllib

from autostop_manager.catalog_clients import PARTSAPI_OPERATIONS


ROOT = Path(__file__).resolve().parents[1]


def test_codex_native_startup_files_are_present_and_safe():
    agents_path = ROOT / "AGENTS.md"
    config_path = ROOT / ".codex" / "config.toml"
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    index = (ROOT / "docs" / "agent" / "knowledge_base_index.md").read_text(encoding="utf-8")

    assert not (ROOT / "agent.md").exists()
    assert agents_path.is_file()
    assert config_path.is_file()

    agents = agents_path.read_text(encoding="utf-8")
    assert len(agents.encode("utf-8")) < 32 * 1024
    for expected in [
        "AutoStop CRM is the source of truth",
        "Gmail is the source of truth",
        "AutostopManager stores only durable non-CRM memory",
        "bootstrap_context",
        "manager_board_scan",
        "dry-run",
        "manager run ledger",
        "Приберись",
        "ready unpaid",
        "Timer floor",
        "crm_vin_oem_parts_lookup_playbook.md",
        "business_document_quality_playbook.md",
        "knowledge-sync",
        "knowledge-audit",
        "annotations-audit",
        "skills-audit",
        "cleanup-audit",
        "docs/agent/autostop_manager_skill.md",
    ]:
        assert expected in agents

    config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert config["project_doc_max_bytes"] == 65536
    assert config["project_doc_fallback_filenames"] == ["AGENTS.md", "README.md"]
    assert config["mcp_servers"]["autostopcrm"]["url"] == "https://crm.autostopcrm.ru/mcp"
    assert config["mcp_servers"]["autostopcrm"]["enabled"] is True
    assert config["mcp_servers"]["autostopcrm"]["tool_timeout_sec"] == 90

    forbidden_config_keys = {
        "approval_policy",
        "sandbox_mode",
        "model",
        "model_provider",
        "model_providers",
        "openai_base_url",
        "chatgpt_base_url",
        "otel",
        "auth",
        "profiles",
        "profile",
    }
    assert forbidden_config_keys.isdisjoint(config)
    config_text = config_path.read_text(encoding="utf-8").casefold()
    assert not re.search(r"(api[_-]?key|token|secret|password|credential)", config_text)

    assert "`AGENTS.md` - canonical compact startup instruction for Codex." in readme
    assert "`AGENTS.md` - canonical compact startup instruction for Codex." in index
    assert "agent.md" not in readme
    assert "agent.md" not in index


def test_home_pc_remote_access_is_documented_as_current_capability():
    checked_paths = [
        ROOT / "AGENTS.md",
        ROOT / "README.md",
        ROOT / "docs" / "agent" / "knowledge_base_index.md",
        ROOT / "docs" / "agent" / "knowledge_shelves.md",
        ROOT / "docs" / "agent" / "codex_home_pc_reverse_ssh.md",
        ROOT / "docs" / "agent" / "knowledge_annotations.jsonl",
        ROOT / "docs" / "agent" / "knowledge_map.json",
        ROOT / "docs" / "agent" / "manager_rules.json",
    ]
    combined = "\n".join(path.read_text(encoding="utf-8") for path in checked_paths)

    for expected in [
        "home-pc",
        "ssh home-pc",
        "private runtime",
        "no public SSH",
        "sftp",
        "scp",
    ]:
        assert expected in combined

    for forbidden in [
        "DESKTOP-BUSO4I8",
        "127.0.0.1:22220",
        "codex-home-tunnel",
        "codexadmin",
        "PowerShell 7.6.3",
        "Python 3.14.6",
    ]:
        assert forbidden not in combined

    route = json.loads((ROOT / "docs" / "agent" / "knowledge_map.json").read_text(encoding="utf-8"))
    assert "remote_codex_access" in route["domains"]
    assert "docs/agent/codex_home_pc_reverse_ssh.md" in route["domains"]["remote_codex_access"]["source_of_truth_files"]


def test_documentation_hygiene_keeps_docs_compact_and_requires_cleanup_audit():
    checked_paths = [
        ROOT / "AGENTS.md",
        ROOT / "README.md",
        ROOT / "docs" / "agent" / "knowledge_shelves.md",
        ROOT / "docs" / "agent" / "autostop_manager_skill.md",
        ROOT / "docs" / "agent" / "knowledge_annotations.jsonl",
    ]
    combined = "\n".join(path.read_text(encoding="utf-8") for path in checked_paths)

    for expected in [
        "cleanup-audit",
        "knowledge-sync",
        "Keep docs compact",
        "Prefer updating an existing canonical file",
        "Delete a tracked doc only when all are true",
    ]:
        assert expected in combined

    route = json.loads((ROOT / "docs" / "agent" / "knowledge_map.json").read_text(encoding="utf-8"))
    knowledge_intake = route["domains"]["knowledge_intake"]
    assert "cleanup-audit" in knowledge_intake["keywords"]
    assert "удалить устаревшие инструкции" in knowledge_intake["keywords"]


def test_board_cleanup_docs_do_not_reintroduce_old_archive_or_description_preview_policy():
    checked_paths = [
        ROOT / "docs" / "agent" / "board_cleanup_autopilot_playbook.md",
        ROOT / "docs" / "agent" / "autostop_manager_skill.md",
        ROOT / "docs" / "agent" / "command_routes.json",
        ROOT / "docs" / "agent" / "knowledge_annotations.jsonl",
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
    route = json.loads((ROOT / "docs" / "agent" / "command_routes.json").read_text(encoding="utf-8"))
    manager_catalog = json.loads((ROOT / "docs" / "agent" / "manager_mcp_catalog.json").read_text(encoding="utf-8"))
    crm_catalog = json.loads((ROOT / "docs" / "agent" / "crm_mcp_catalog.json").read_text(encoding="utf-8"))

    assert "This playbook is the only detailed source of truth" in playbook
    assert "leave it empty" in playbook
    assert "phone goes to the client" in playbook
    assert "VIN/plate/mileage" in playbook
    assert "vehicle` as a compact make/model" in playbook
    assert "no more than three tags" in playbook
    assert "Bad public `description` patterns" in playbook
    assert "repair_orders_changed=0 and payments_changed=0" in playbook

    cleanup_route = next(item for item in route["routes"] if item["command_id"] == "board_cleanup_autopilot")
    route_text = "\n".join(cleanup_route["next_actions"])
    assert "if description is empty leave it empty" in route_text
    assert "tags rare with no more than three" in route_text
    assert "move phone/VIN/plate/mileage/aggregates" in route_text

    command = manager_catalog["natural_language_commands"]["Приберись"]
    assert command["aliases"] == ["Приберись"]
    assert any("no more than three operational tags" in item for item in command["allowed_actions"])
    assert any("invent text for an empty public description" in item for item in command["forbidden_actions"])

    assert "cleanup_card_content" in crm_catalog["not_mcp_runtime_tools"]
    assert any(
        "leave empty descriptions empty" in item and "tags rare and capped at three" in item
        for item in crm_catalog["operation_notes"]
    )


def test_business_documents_route_requires_crm_print_module_for_autostop_documents():
    playbook = (ROOT / "docs" / "agent" / "business_document_quality_playbook.md").read_text(encoding="utf-8")
    index = (ROOT / "docs" / "agent" / "knowledge_base_index.md").read_text(encoding="utf-8")
    annotations = (ROOT / "docs" / "agent" / "knowledge_annotations.jsonl").read_text(encoding="utf-8")
    manager_rules = json.loads((ROOT / "docs" / "agent" / "manager_rules.json").read_text(encoding="utf-8"))
    crm_catalog = json.loads((ROOT / "docs" / "agent" / "crm_mcp_catalog.json").read_text(encoding="utf-8"))
    business_document_rule = next(
        rule for rule in manager_rules["rules"] if rule["id"] == "business-document-quality-gate"
    )["rule"]

    combined = "\n".join([playbook, index, annotations])
    assert "CRM print module" in combined
    assert "create_document_without_card_pdf" in combined
    assert "download_repair_order_print_pdf" in combined
    assert "standard AutoStop templates" in combined
    assert "tax_label" in combined
    assert "Без НДС" in combined
    assert "do not build independent PDF/HTML templates" in combined
    assert "Документ без карточки" in combined
    assert "infer the standard document type" in combined
    assert "CRM print module" in business_document_rule
    assert "standard AutoStop templates" in business_document_rule
    assert "create_document_without_card_pdf" in business_document_rule
    assert "download_repair_order_print_pdf" in business_document_rule
    assert "tax_label" in business_document_rule
    assert "Без НДС" in business_document_rule
    assert "Do not build independent PDF/HTML templates" in business_document_rule
    assert "create_document_without_card_pdf" in crm_catalog["tool_families"]["repair_order"]
    assert any("documents without CRM cards" in note for note in crm_catalog["operation_notes"])
    assert "optional document_type" in crm_catalog["schema_notes"]["autostop_document_printing"]
    assert "tax_label" in crm_catalog["schema_notes"]["autostop_document_printing"]


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


def test_source_pack_playbook_navigation_uses_repo_relative_paths():
    checked_paths = [
        ROOT / "docs" / "agent" / "bmw_repair_playbook.md",
        ROOT / "docs" / "agent" / "ecu_calibration_programming_playbook.md",
        ROOT / "docs" / "agent" / "parts_search_playbook.md",
    ]
    ambiguous_prefixes = ("`data/", "`md/", "`markdown/", "`sources/")
    offenders: list[str] = []
    for path in checked_paths:
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if any(prefix in line for prefix in ambiguous_prefixes):
                offenders.append(f"{path.relative_to(ROOT)}:{line_number}")

    assert offenders == []


def test_3d_printing_playbook_has_linux_and_powershell_command_paths():
    playbook = (ROOT / "docs" / "agent" / "3d_printing_cad_playbook.md").read_text(encoding="utf-8")

    assert "```powershell" in playbook
    assert "```bash" in playbook
    assert "$AUTOSTOP_3D_WORKSPACE" in playbook
    assert ".venv/bin/python scripts/cad.py check" in playbook


def test_bmw_jsonl_indexes_keep_canonical_lookup_fields():
    data_root = ROOT / "docs" / "agent" / "automotive_sources" / "source_cache" / "bmw_repair_knowledge_pack" / "data"
    required = {
        "bmw_chassis_codes.jsonl": ("chassis", "body_code"),
        "bmw_control_units_glossary.jsonl": ("abbreviation", "system", "meaning"),
        "bmw_transmission_families.jsonl": ("transmission", "system", "notes_ru"),
    }

    for filename, fields in required.items():
        rows = [
            json.loads(line) for line in (data_root / filename).read_text(encoding="utf-8").splitlines() if line.strip()
        ]
        missing = [
            (index, field) for index, row in enumerate(rows, 1) for field in fields if row.get(field) in (None, "")
        ]
        assert missing == []


def test_partsapi_contract_docs_match_adapter_operations():
    contract = (ROOT / "docs" / "agent" / "partsapi_method_contracts.md").read_text(encoding="utf-8")
    source_registry = json.loads((ROOT / "docs" / "agent" / "vin_oem_sources.json").read_text(encoding="utf-8"))
    source_text = json.dumps(source_registry, ensure_ascii=False)

    for operation, spec in PARTSAPI_OPERATIONS.items():
        assert operation in contract
        assert spec["method"] in contract
        assert spec["method"] in source_text


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
        re.compile(r"(?<!\d)(?:\+?7|8)\s?[\d\s() -]{9,18}"),
    ]
    for raw_path in tracked_docs:
        path = ROOT / raw_path
        if not path.is_file():
            continue
        content = path.read_text(encoding="utf-8", errors="ignore")
        if any(pattern.search(content) for pattern in blocked_patterns):
            offenders.append(raw_path)

    assert offenders == []
