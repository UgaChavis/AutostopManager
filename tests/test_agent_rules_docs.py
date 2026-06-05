from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess

from autostop_manager.catalog_clients import PARTSAPI_OPERATIONS


ROOT = Path(__file__).resolve().parents[1]


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
        "прибейсь",
        "переберись",
    ]

    combined = "\n".join(path.read_text(encoding="utf-8").casefold() for path in checked_paths)

    for fragment in stale_fragments:
        assert fragment not in combined
    assert "board_summary" in combined
    assert "separate explicit owner command" in combined


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
    blocked_fragments = [
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
        ROOT / "docs" / "agent" / "ai_parts_krasnoyarsk_playbook.md",
    ]
    ambiguous_prefixes = ("`data/", "`md/", "`markdown/", "`sources/")
    offenders: list[str] = []
    for path in checked_paths:
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if any(prefix in line for prefix in ambiguous_prefixes):
                offenders.append(f"{path.relative_to(ROOT)}:{line_number}")

    assert offenders == []


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
