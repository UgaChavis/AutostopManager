from __future__ import annotations

import csv
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

    assert not (ROOT / "agent.md").exists()
    assert agents_path.is_file()
    assert config_path.is_file()

    agents = agents_path.read_text(encoding="utf-8")
    assert len(agents.encode("utf-8")) < 32 * 1024
    for expected in [
        "AutoStop CRM is the source of truth",
        "Gmail is the source of truth",
        "AutostopManager stores only durable non-CRM memory",
        "agent_bootstrap",
        "agent_board_digest",
        "dry_run",
        "Gateway v2 workflow ledger",
        "Docker `.Config.Env`",
        "Use `knowledge-probe` only for focused document lookup",
        "it never grants writes, connector access or financial authority",
        "exactly 77 tools",
        "24-tool Gateway v2 connector",
        "knowledge-sync",
        "knowledge-audit",
        "skills-audit",
        "cleanup-audit",
        "Store work is paused",
        "raw CRM/Store/Gmail/Telegram exports",
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
    assert "agent.md" not in readme

    voice_brief = (ROOT / "docs" / "agent" / "voice_agent_brief.md").read_text(encoding="utf-8")
    assert "дополнение к `AGENTS.md`, а не копия" in voice_brief
    assert "Выполни один" in voice_brief
    assert "`agent-brief` для фактической первой команды" in voice_brief
    assert "подготовительный вызов не нужен" in voice_brief


def test_home_pc_remote_access_is_documented_as_current_capability():
    checked_paths = [
        ROOT / "AGENTS.md",
        ROOT / "README.md",
        ROOT / "docs" / "agent" / "codex_home_pc_reverse_ssh.md",
        ROOT / "docs" / "agent" / "knowledge_map.json",
        ROOT / "docs" / "agent" / "manager_rules.json",
    ]
    combined = "\n".join(path.read_text(encoding="utf-8") for path in checked_paths)

    for expected in [
        "home-pc",
        "DESKTOP-BUSO4I8",
        "127.0.0.1:22220",
        "codex-home-tunnel",
        "codexadmin",
        "do not rotate",
        "no public home SSH",
        "sftp",
        "scp",
        "pwsh",
        "write-public-desktop-note.ps1",
        "open-in-user-session.ps1",
        "managed-pc refresh-device-files",
        "ControlPersist 600",
        "127.0.0.1:9223",
        "/root/.codex/CODEX_VPN_FST_ACCESS.md",
        "autostop-vpn-fst",
        "autostop-vps27560",
        "StrictHostKeyChecking=no",
        "host-key mismatch",
        "route CRM traffic through a VPN",
    ]:
        assert expected in combined

    assert "no `health-check.ps1` is installed" in combined

    access_doc = (ROOT / "docs" / "agent" / "codex_home_pc_reverse_ssh.md").read_text(encoding="utf-8")
    assert len(access_doc.splitlines()) <= 180
    assert "AutostopVPN Daily Deep Check" not in access_doc
    assert "static Cloudflare resolvers" not in access_doc

    route = json.loads((ROOT / "docs" / "agent" / "knowledge_map.json").read_text(encoding="utf-8"))
    assert "remote_codex_access" in route["domains"]
    assert "docs/agent/codex_home_pc_reverse_ssh.md" in route["domains"]["remote_codex_access"]["primary_files"]


def test_documentation_hygiene_keeps_docs_compact_and_requires_cleanup_audit():
    checked_paths = [
        ROOT / "AGENTS.md",
        ROOT / "README.md",
    ]
    combined = "\n".join(path.read_text(encoding="utf-8") for path in checked_paths)

    for expected in [
        "cleanup-audit",
        "knowledge-sync",
        "Compact startup contract",
        "Keep one canonical owner per rule",
    ]:
        assert expected in combined

    knowledge_map = json.loads((ROOT / "docs" / "agent" / "knowledge_map.json").read_text(encoding="utf-8"))
    allowed = {"title", "primary_files", "reference_files", "optional_runtime_files", "skill_path"}
    assert knowledge_map["format"] == "knowledge_navigation_v1"
    assert all(set(domain) <= allowed for domain in knowledge_map["domains"].values())
    assert not (ROOT / "docs" / "agent" / "knowledge_annotations.jsonl").exists()


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
    payload = json.loads((ROOT / "docs/agent/manager_rules.json").read_text(encoding="utf-8"))

    assert payload["format"] == "manager_runtime_invariants_v2"
    assert {rule["id"] for rule in payload["rules"]} == {
        "source-boundaries",
        "command-knowledge-separation",
        "store-owner-pause",
        "guarded-write-lifecycle",
        "financial-and-external-authority",
        "workflow-recovery",
        "release-boundary",
    }


def test_redundant_navigation_and_generated_source_maps_stay_removed():
    removed = [
        "docs/agent/knowledge_base_index.md",
        "docs/agent/knowledge_intake_playbook.md",
        "docs/agent/ai_parts_krasnoyarsk_playbook.md",
        "docs/agent/zzap_search_playbook.md",
        "docs/agent/manager_identity.json",
        "docs/agent/memory_policy.json",
        "docs/agent/phone_flow.json",
        "docs/agent/automotive_sources/brand_source_map.json",
        "docs/agent/automotive_sources/data_type_source_map.json",
        "docs/agent/automotive_sources/dsg_transmission_sources.json",
        "docs/agent/automotive_sources/model_source_overrides.json",
        "docs/agent/gmail_mcp_catalog.json",
        "docs/agent/knowledge_annotations.jsonl",
    ]

    assert all(not (ROOT / path).exists() for path in removed)


def test_command_registry_v3_contains_only_operational_routing_metadata():
    payload = json.loads((ROOT / "docs" / "agent" / "command_routes.json").read_text(encoding="utf-8"))

    assert payload["format"] == "agent_command_registry_v3"
    command_ids = [route["command_id"] for route in payload["routes"]]
    assert len(command_ids) == len(set(command_ids))
    allowed = {
        "command_id",
        "workflow_id",
        "intent",
        "priority",
        "phase",
        "knowledge_domains",
        "effects",
        "dependencies",
        "signals",
    }
    forbidden = {
        "open_first",
        "memory_queries",
        "required_reads",
        "write_domains",
        "external_connectors",
        "completion_checks",
        "next_actions",
        "read_entity_selection",
        "operation_selection",
        "aliases",
        "keywords",
    }
    for route in payload["routes"]:
        assert set(route) <= allowed
        assert forbidden.isdisjoint(route)
        assert isinstance(route["knowledge_domains"], list)
        assert isinstance(route["effects"], list)
        assert isinstance(route["dependencies"], list)
        assert set(route["signals"]) <= {"phrases", "all", "any", "exclude"}


def test_service_director_mode_has_one_canonical_route_and_guarded_autonomy():
    manifest = (ROOT / "docs" / "agent" / "service_director_manifest.md").read_text(encoding="utf-8")
    knowledge_map = json.loads((ROOT / "docs" / "agent" / "knowledge_map.json").read_text(encoding="utf-8"))
    command_routes = json.loads((ROOT / "docs" / "agent" / "command_routes.json").read_text(encoding="utf-8"))

    assert "Повышать прибыльность и производительность AutoStop" in manifest
    assert "жесткому шаблону" in manifest
    assert "next_review_at" in manifest
    assert "повторно не спрашивать" in manifest
    assert "get_card_log" in manifest
    assert "Циклическую директорскую цель завершать только по команде владельца" in manifest
    director_domain = knowledge_map["domains"]["service_director"]
    assert director_domain["skill_path"] == ".agents/skills/run-autostop-director/SKILL.md"
    director_route = next(
        route for route in command_routes["routes"] if route["command_id"] == "service_director_cycle"
    )
    assert director_route["workflow_id"] == "service_director_cycle"
    assert "service_director" in director_route["knowledge_domains"]


def test_mcp_catalogs_are_minimal_verified_surface_manifests():
    manager_catalog = json.loads((ROOT / "docs" / "agent" / "manager_mcp_catalog.json").read_text(encoding="utf-8"))
    crm_catalog = json.loads((ROOT / "docs" / "agent" / "crm_mcp_catalog.json").read_text(encoding="utf-8"))
    expected_keys = {
        "format",
        "source",
        "expected_tool_count",
        "expected_tool_names",
        "schema_fingerprint",
        "verified_at",
    }
    for catalog, expected_count in ((manager_catalog, 77), (crm_catalog, 24)):
        assert set(catalog) == expected_keys
        assert catalog["format"] == "mcp_surface_manifest_v1"
        names = catalog["expected_tool_names"]
        assert names == sorted(set(names))
        assert catalog["expected_tool_count"] == len(names) == expected_count
        assert re.fullmatch(r"[0-9a-f]{64}", catalog["schema_fingerprint"])
        assert "sha256(canonical sorted [{name,inputSchema}])" in catalog["source"]

    assert "audit_knowledge_annotations" in manager_catalog["expected_tool_names"]
    assert "agent_finance_workflow" in crm_catalog["expected_tool_names"]


def test_store_procedures_live_in_playbook_not_route_or_catalog():
    routes_payload = json.loads((ROOT / "docs" / "agent" / "command_routes.json").read_text(encoding="utf-8"))
    manager_catalog = (ROOT / "docs" / "agent" / "manager_mcp_catalog.json").read_text(encoding="utf-8")
    playbook = (ROOT / "docs" / "agent" / "store_management_playbook.md").read_text(encoding="utf-8")
    routes = {route["command_id"]: route for route in routes_payload["routes"]}

    assert {"store_read_workflow", "store_management_workflow"} <= set(routes)
    assert "store_management" in routes["store_management_workflow"]["knowledge_domains"]
    assert "planned_changes_by_action" not in manager_catalog
    for required in [
        'agent_board_digest(scope="store")',
        "assign_quote_request",
        "assignee_id",
        "update_quote_request_comment",
        "internal_comment",
        "replace_quote_offer_drafts",
        "storage_location",
        "store_owner_api",
    ]:
        assert required in playbook

    workflow = (ROOT / ".github" / "workflows" / "quality.yml").read_text(encoding="utf-8")
    for expected in [
        'python-version: "3.11"',
        'pip install -e ".[dev]"',
        "knowledge-sync",
        "knowledge-audit",
        "skills-audit",
        "cleanup-audit",
        "ruff check .",
        "ruff format --check autostop_manager tests",
        "mypy autostop_manager",
        "pytest -q",
        "coverage report --fail-under=82",
        "workflow_dispatch",
        "pull_request",
    ]:
        assert expected in workflow


def test_gmail_playbook_lists_current_documented_surface():
    playbook = (ROOT / "docs" / "agent" / "gmail_workflow_playbook.md").read_text(encoding="utf-8")
    tools = [
        "_get_profile",
        "_list_labels",
        "_search_emails",
        "_search_email_ids",
        "_read_email",
        "_batch_read_email",
        "_read_email_thread",
        "_batch_read_email_threads",
        "_list_drafts",
        "_read_attachment",
        "_create_label",
        "_apply_labels_to_emails",
        "_batch_modify_email",
        "_bulk_label_matching_emails",
        "_archive_emails",
        "_delete_emails",
        "_create_draft",
        "_update_draft",
        "_send_draft",
        "_send_email",
        "_forward_emails",
    ]

    assert len(tools) == len(set(tools)) == 21
    assert all(tool in playbook for tool in tools)
    assert "active connector registration is the schema source of truth" in playbook
    for stale_field in ["attachment_files", "body_file", "html_body", "content_type"]:
        assert stale_field not in playbook


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
    assert "complete, coherent and gradually developing story" in description_standard
    assert "There are no mandatory headings, blocks, dates, line counts" in description_standard
    assert "one or two natural sentences" in description_standard
    assert "reread the entire existing description" in description_standard
    assert "what was found, what was agreed, what was done" in description_standard
    assert "fresh natural-language" in description_standard
    assert "complaint, findings and diagnostic results" in description_standard
    assert "Do not merely append the latest event" in description_standard
    assert "phone goes to the client" in playbook
    assert "VIN/plate/mileage" in playbook
    assert "vehicle` as a compact make/model" in playbook
    assert "no more than three tags" in playbook
    assert "Facts To Preserve And Exclude" in description_standard
    assert "repair_orders_changed=0 and payments_changed=0" in playbook

    cleanup_route = next(item for item in route["routes"] if item["command_id"] == "board_cleanup_autopilot")
    assert {"board_cleanup_autopilot", "crm_card_description_standard"} <= set(cleanup_route["knowledge_domains"])
    assert "crm_write" in cleanup_route["effects"]


def test_director_journal_contract_is_documented_and_bounded():
    skill = (ROOT / ".agents/skills/run-autostop-director/SKILL.md").read_text(encoding="utf-8")
    manifest = (ROOT / "docs/agent/service_director_manifest.md").read_text(encoding="utf-8")
    crm_playbook = (ROOT / "docs/agent/crm_manager_data_playbook.md").read_text(encoding="utf-8")
    routes = json.loads((ROOT / "docs/agent/command_routes.json").read_text(encoding="utf-8"))

    assert "service_director_manifest.md" in skill
    assert "## Единый директорский журнал" in manifest
    assert "data/autostop_manager.sqlite3" in manifest
    assert "50" in manifest and "400" in manifest
    assert "не более 180 дней" in manifest
    assert "не более 600 символов" in manifest
    assert "director_create" in manifest
    assert "expected_updated_at" in manifest
    assert "workflow_ref_hash" in manifest
    assert "до `next_review_at` повторно не спрашивать" in manifest
    assert "service_director_manifest.md" in crm_playbook
    director_route = next(item for item in routes["routes"] if item["command_id"] == "service_director_cycle")
    assert "service_director" in director_route["knowledge_domains"]
    assert "crm_write" in director_route["effects"]


def test_business_documents_route_requires_crm_print_module_for_autostop_documents():
    playbook = (ROOT / "docs" / "agent" / "business_document_quality_playbook.md").read_text(encoding="utf-8")
    routes = json.loads((ROOT / "docs" / "agent" / "command_routes.json").read_text(encoding="utf-8"))

    for expected in [
        "CRM print module",
        "create_document_without_card_pdf",
        "download_repair_order_print_pdf",
        "standard AutoStop template",
        "tax_label",
        "Без НДС",
        "Do not build independent PDF/HTML templates",
        "Документ без карточки",
        "infer the standard document type",
    ]:
        assert expected in playbook

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


def test_compacted_source_pack_manifests_match_retained_files():
    cache_root = ROOT / "docs" / "agent" / "automotive_sources" / "source_cache"
    bmw_root = cache_root / "bmw_repair_knowledge_pack"
    bmw_manifest = json.loads((bmw_root / "manifest.json").read_text(encoding="utf-8"))
    bmw_files = sorted(
        path.relative_to(bmw_root).as_posix()
        for path in bmw_root.rglob("*")
        if path.is_file() and path.name != "manifest.json"
    )

    offline_root = cache_root / "offline_parts_catalogs_knowledge_pack"

    assert bmw_manifest["file_count_excluding_manifest"] == len(bmw_files)
    assert bmw_manifest["files"] == bmw_files
    assert not (cache_root / "ai_parts_krasnoyarsk_project_pack").exists()
    assert not (offline_root / "sources" / "citations.md").exists()
    assert (offline_root / "sources" / "offline_parts_catalog_sources.json").is_file()


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


def test_source_pack_playbook_navigation_uses_repo_relative_paths():
    checked_paths = [
        ROOT / "docs" / "agent" / "bmw_repair_playbook.md",
        ROOT / "docs" / "agent" / "ecu_calibration_programming_playbook.md",
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
