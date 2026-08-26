from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from .catalog_adapters import catalog_provider_status
from .config import PROJECT_ROOT
from .knowledge_base import audit_knowledge_base
from .memory_curator import audit_memory
from .storage import ManagerMemoryStore, _now
from .system_audit import build_system_audit


MANAGER_MCP_CATALOG_PATH = PROJECT_ROOT / "docs" / "agent" / "manager_mcp_catalog.json"
CRM_MCP_CATALOG_PATH = PROJECT_ROOT / "docs" / "agent" / "crm_mcp_catalog.json"
CRM_ROOT = Path("/opt/autostopcrm")
CODEX_SYSTEM_SKILLS_ROOT = Path("/root/.codex/skills/.system")
CODEX_PLUGIN_CACHE_ROOT = Path("/root/.codex/plugins/cache/openai-curated")
CODEX_STANDALONE_CURRENT = Path("/root/.codex/packages/standalone/current")
CORE_TOOL_COMMANDS = {
    "python3": [["python3", "--version"]],
    "git": [["git", "--version"]],
    "gh": [["gh", "--version"]],
    "docker": [["docker", "--version"]],
    "docker_compose": [["docker", "compose", "version"]],
    "curl": [["curl", "--version"]],
    "ss": [["ss", "--version"]],
    "pdftotext": [["pdftotext", "-v"]],
    "pdftoppm": [["pdftoppm", "-v"]],
    "ghostscript": [["gs", "--version"]],
    "libreoffice": [["libreoffice", "--version"]],
    "7z": [["7z"]],
    "node": [["node", "--version"]],
    "npm": [["npm", "--version"]],
    "chromium": [["chromium", "--version"], ["chromium-browser", "--version"], ["google-chrome", "--version"]],
    "nginx": [["nginx", "-v"]],
    "pnpm": [["pnpm", "--version"]],
    "pwsh": [["pwsh", "--version"]],
}
REQUIRED_CORE_TOOLS = {
    "python3",
    "git",
    "docker",
    "docker_compose",
    "curl",
    "ss",
    "pdftotext",
    "pdftoppm",
    "ghostscript",
    "libreoffice",
    "7z",
    "node",
    "npm",
    "chromium",
}
VENV_PACKAGES = ["mcp", "pytest", "ruff", "pre-commit", "reportlab", "PyMuPDF", "playwright", "requests"]
SECRET_PATTERNS = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in [
        r"sk-[A-Za-z0-9_\-]{12,}",
        r"ghp_[A-Za-z0-9_]{12,}",
        r"(\b(?:[A-Z0-9]+_)*(?:TOKEN|SECRET|PASSWORD|KEY)(?:[0-9]+|_[A-Z0-9]+)*\s*[:=]\s*)(?:\"[^\"\n]*\"|'[^'\n]*'|[^\s]+)",
    ]
]


def build_control_report(
    *,
    store: ManagerMemoryStore | None = None,
    project_root: Path | str = PROJECT_ROOT,
) -> dict[str, Any]:
    root = Path(project_root)
    memory = store or ManagerMemoryStore()
    memory.initialize()

    system_audit = build_system_audit(store=memory, project_root=root)
    git = _git_state(root)
    knowledge = _knowledge_summary(memory)
    memory_summary = _memory_summary(memory)
    learning = memory.get_agent_learning_summary(limit=5)
    mcp = _mcp_catalog_summary()
    providers = _provider_summary()
    ports = _public_ports()
    server_environment = _server_environment(root, ports=ports)
    codex_readiness = _codex_readiness(root)
    runtime_readiness = _runtime_readiness(root, memory=memory)
    provider_readiness = _provider_readiness(providers)
    production_ops = _production_ops(root)
    production = _production_health(root, production_ops=production_ops)
    tests = _tests_doctor_status(root)
    ledger = memory.list_manager_runs(limit=5, include_events=False)
    open_risk = _open_risk_score(
        git=git,
        providers=providers,
        server_environment=server_environment,
        codex_readiness=codex_readiness,
        runtime_readiness=runtime_readiness,
        production_ops=production_ops,
        ports=ports,
    )
    risks = _collect_risks(
        system_audit=system_audit,
        git=git,
        knowledge=knowledge,
        memory=memory_summary,
        mcp=mcp,
        providers=providers,
        server_environment=server_environment,
        codex_readiness=codex_readiness,
        runtime_readiness=runtime_readiness,
        production=production,
        production_ops=production_ops,
        ports=ports,
        open_risk=open_risk,
    )
    status = _traffic_status(risks)
    report = {
        "ok": status != "red",
        "schema": "ControlReportV1",
        "generated_at": _now(),
        "summary": {
            "status": status,
            "risk_count": len(risks),
            "red_risks": sum(1 for risk in risks if risk["severity"] == "red"),
            "yellow_risks": sum(1 for risk in risks if risk["severity"] == "yellow"),
            "memory_total": memory_summary["total_count"],
            "knowledge_documents": knowledge["documents_indexed"],
            "mcp_manager_tools": mcp["manager"].get("tool_count"),
            "providers_configured": providers["configured_count"],
            "providers_total": providers["provider_count"],
            "open_risk_score": open_risk["score"],
            "open_risk_level": open_risk["level"],
            "agent_execution_mode": ((learning.get("mode") or {}).get("global_mode") or "work"),
        },
        "system_health": {
            "ok": bool(system_audit.get("ok")),
            "summary": system_audit.get("summary") or {},
            "warnings": system_audit.get("warnings") or [],
        },
        "server_environment": server_environment,
        "codex_readiness": codex_readiness,
        "runtime_readiness": runtime_readiness,
        "git": git,
        "tests_doctor": tests,
        "memory": memory_summary,
        "learning": learning,
        "knowledge": knowledge,
        "mcp": mcp,
        "providers": providers,
        "provider_readiness": provider_readiness,
        "production": production,
        "production_ops": production_ops,
        "open_risk": open_risk,
        "ports": ports,
        "risks": risks,
        "last_run_ledger": {
            "items": ledger.get("items", []),
            "total_returned": ledger.get("total_returned", 0),
        },
        "privacy": {
            "secrets_redacted": True,
            "crm_writes": False,
            "supplier_orders": False,
            "email_sends": False,
            "dashboard_public_service": False,
        },
    }
    return _redact_payload(report)


def format_control_report_markdown(report: dict[str, Any]) -> str:
    summary = report.get("summary") or {}
    lines = [
        "# AutoStopManager Control Report",
        "",
        f"- Schema: `{report.get('schema', 'ControlReportV1')}`",
        f"- Generated: `{report.get('generated_at', '')}`",
        f"- Status: `{summary.get('status', 'unknown')}`",
        f"- Risks: `{summary.get('risk_count', 0)}`",
        f"- Open risk score: `{summary.get('open_risk_score', 0)}` (`{summary.get('open_risk_level', 'unknown')}`)",
        "",
        "## Git",
        f"- Branch: `{(report.get('git') or {}).get('branch', 'unknown')}`",
        f"- Dirty: `{(report.get('git') or {}).get('dirty', False)}`",
        "",
        "## Server Environment",
        f"- OS: `{((report.get('server_environment') or {}).get('os') or {}).get('pretty_name', 'unknown')}`",
        f"- Kernel: `{((report.get('server_environment') or {}).get('os') or {}).get('kernel', 'unknown')}`",
        f"- /tmp writable: `{((report.get('server_environment') or {}).get('paths') or {}).get('tmp_writable', False)}`",
        f"- Required tools present: `{((report.get('server_environment') or {}).get('core_tools') or {}).get('required_present_count', 0)}`/`{len(REQUIRED_CORE_TOOLS)}`",
        f"- Public listeners: `{len((report.get('ports') or {}).get('public_listeners') or [])}`",
        "",
        "## Runtime Readiness",
        f"- Manager venv: `{((report.get('runtime_readiness') or {}).get('manager_venv') or {}).get('ok', False)}`",
        f"- CRM venv: `{((report.get('runtime_readiness') or {}).get('crm_venv') or {}).get('ok', False)}`",
        f"- Browser/document tools: `{((report.get('runtime_readiness') or {}).get('browser_document_tools') or {}).get('ok', False)}`",
        f"- Manager env file: `{(((report.get('runtime_readiness') or {}).get('env_files') or {}).get('manager') or {}).get('present', False)}`",
        f"- CRM env file: `{(((report.get('runtime_readiness') or {}).get('env_files') or {}).get('crm') or {}).get('present', False)}`",
        "",
        "## Codex Readiness",
        f"- CLI version: `{(((report.get('codex_readiness') or {}).get('runtime') or {}).get('active_version', 'unknown'))}`",
        f"- Stale app-server processes: `{len(((report.get('codex_readiness') or {}).get('runtime') or {}).get('stale_app_server_processes') or [])}`",
        f"- System skills: `{((report.get('codex_readiness') or {}).get('skills') or {}).get('system_skill_count', 0)}`",
        f"- Plugin skills: `{((report.get('codex_readiness') or {}).get('skills') or {}).get('plugin_skill_count', 0)}`",
        f"- Manager MCP import: `{(((report.get('codex_readiness') or {}).get('mcp_imports') or {}).get('manager') or {}).get('ok', False)}`",
        f"- CRM MCP import: `{(((report.get('codex_readiness') or {}).get('mcp_imports') or {}).get('crm') or {}).get('ok', False)}`",
        "",
        "## Health",
        f"- System audit OK: `{(report.get('system_health') or {}).get('ok', False)}`",
        f"- Knowledge docs: `{summary.get('knowledge_documents', 0)}`",
        f"- Memory total: `{summary.get('memory_total', 0)}`",
        f"- Agent execution mode: `{summary.get('agent_execution_mode', 'work')}`",
        f"- MCP manager tools: `{summary.get('mcp_manager_tools', 0)}`",
        f"- Providers configured: `{summary.get('providers_configured', 0)}/{summary.get('providers_total', 0)}`",
        f"- Provider external access backlog: `{((report.get('provider_readiness') or {}).get('external_access_backlog_count', 0))}`",
        "",
        "## Learning Mode",
        f"- Global mode: `{(((report.get('learning') or {}).get('mode') or {}).get('global_mode', 'work'))}`",
        f"- Recent improvement candidates: `{len((report.get('learning') or {}).get('recent_improvements') or [])}`",
        "",
        "## Provider Matrix",
    ]
    for row in (report.get("providers") or {}).get("stage_matrix") or []:
        lines.append(
            f"- `{row['stage']}`: `{row['configured_count']}` configured, `{row['live_callable_count']}` live-readable"
        )
    production_ops = report.get("production_ops") or {}
    lines.extend(
        [
            "",
            "## Production Ops",
            f"- Compose config: `{((production_ops.get('compose') or {}).get('config') or {}).get('ok', False)}`",
            f"- Nginx config: `{((production_ops.get('nginx') or {}).get('config') or {}).get('ok', False)}`",
            f"- Watchdog timer: `{(((production_ops.get('watchdog') or {}).get('timer') or {}).get('active_state', 'unknown'))}`",
            f"- Watchdog policy: `{(((production_ops.get('watchdog') or {}).get('policy') or {}).get('state', 'unknown'))}`",
            f"- Container health: `{(((production_ops.get('container') or {}).get('autostopcrm') or {}).get('health', 'unknown'))}`",
        ]
    )
    lines.extend(["", "## Risks"])
    risks = report.get("risks") or []
    if not risks:
        lines.append("- None")
    else:
        for risk in risks:
            lines.append(f"- `{risk['severity']}` `{risk['category']}`: {risk['message']}")
    lines.extend(
        [
            "",
            "## Commands",
            "- `python -m autostop_manager.cli control-report --format json`",
            "- `python -m autostop_manager.cli control-report --format markdown --output reports/control-report.md`",
            "- `python -m autostop_manager.cli doctor`",
            "- server/Unix only: `bash scripts/doctor.sh --full`",
        ]
    )
    return "\n".join(lines) + "\n"


def _knowledge_summary(memory: ManagerMemoryStore) -> dict[str, Any]:
    audit = audit_knowledge_base(memory)
    summary = audit.get("summary") or {}
    return {
        "ok": bool(audit.get("ok")),
        "domain_count": int(audit.get("domain_count") or summary.get("route_card_count") or 0),
        "documents_indexed": int(audit.get("documents_indexed") or summary.get("document_count") or 0),
        "section_count": int(audit.get("sections_indexed") or summary.get("section_count") or 0),
        "warnings": list(audit.get("warnings") or []),
    }


def _memory_summary(memory: ManagerMemoryStore) -> dict[str, Any]:
    memory_map = memory.memory_map()
    audit = audit_memory(memory)
    sections = memory_map.get("sections") or {}
    total = sum(int((section or {}).get("count") or 0) for section in sections.values() if isinstance(section, dict))
    return {
        "ok": bool(memory_map.get("ok")) and bool(audit.get("ok")),
        "sections": sections,
        "total_count": total,
        "duplicate_groups": len(audit.get("duplicates") or []),
        "expired_count": len(audit.get("expired") or []),
        "superseded_count": len(audit.get("superseded") or []),
        "warnings": audit.get("warnings") or [],
    }


def _provider_summary() -> dict[str, Any]:
    status = catalog_provider_status()
    providers = status.get("providers") or []
    return {
        "ok": bool(status.get("ok")),
        "provider_count": len(providers),
        "configured_count": int(status.get("configured_count") or 0),
        "live_callable_count": int(status.get("live_callable_count") or 0),
        "missing_provider_ids": status.get("missing_provider_ids") or [],
        "external_access_backlog_count": len(status.get("missing_provider_ids") or []),
        "stage_matrix": status.get("stage_matrix") or [],
        "providers": [
            {
                "source_id": provider.get("source_id"),
                "stage": provider.get("stage"),
                "access_mode": provider.get("access_mode"),
                "configured": bool(provider.get("configured")),
                "live_callable_now": bool(provider.get("live_callable_now")),
                "missing_env_names": provider.get("missing_env_names") or [],
                "manual_allowed": bool(provider.get("manual_allowed")),
            }
            for provider in providers
        ],
    }


def _mcp_catalog_summary() -> dict[str, Any]:
    return {
        "manager": _read_catalog_counts(MANAGER_MCP_CATALOG_PATH),
        "crm": _read_catalog_counts(CRM_MCP_CATALOG_PATH),
    }


def _server_environment(root: Path, *, ports: dict[str, Any]) -> dict[str, Any]:
    core_tools = _core_tools()
    paths = {
        "project_root": str(root),
        "crm_root": str(CRM_ROOT),
        "tmp_writable": _tmp_writable(),
        "running_as_root": os.geteuid() == 0 if hasattr(os, "geteuid") else False,
    }
    required_missing = [
        name
        for name in sorted(REQUIRED_CORE_TOOLS)
        if not ((core_tools.get("tools") or {}).get(name) or {}).get("present")
    ]
    port_classification = _classify_ports(ports)
    return {
        "ok": not required_missing and paths["tmp_writable"],
        "os": {
            "pretty_name": _os_pretty_name(),
            "system": platform.system(),
            "release": platform.release(),
            "kernel": platform.platform(),
            "machine": platform.machine(),
        },
        "python": {
            "executable": sys.executable,
            "version": platform.python_version(),
        },
        "paths": paths,
        "disk": _disk_summary([Path("/"), Path("/opt"), Path("/tmp")]),
        "memory": _proc_memory_summary(),
        "core_tools": core_tools,
        "ports": port_classification,
        "warnings": [f"missing_required_tool:{name}" for name in required_missing],
    }


def _codex_readiness(root: Path) -> dict[str, Any]:
    hooks = {
        "manager": _pre_commit_hook_status(root),
        "crm": _pre_commit_hook_status(CRM_ROOT),
    }
    imports = {
        "manager": _mcp_import_status(root / ".venv" / "bin" / "python", cwd=root),
        "crm": _mcp_import_status(CRM_ROOT / ".venv" / "bin" / "python", cwd=CRM_ROOT),
    }
    skills = _codex_skill_inventory()
    runtime = _codex_runtime_status(root)
    warnings: list[str] = []
    for repo_name, hook in hooks.items():
        if not hook["hook_installed"]:
            warnings.append(f"{repo_name}_pre_commit_hook_missing")
    for env_name, result in imports.items():
        if not result["ok"]:
            warnings.append(f"{env_name}_mcp_import_failed")
    if skills["system_skill_count"] == 0:
        warnings.append("codex_system_skills_missing")
    warnings.extend(runtime.get("warnings") or [])
    return {
        "ok": not warnings,
        "skills": skills,
        "runtime": runtime,
        "hooks": hooks,
        "mcp_imports": imports,
        "mcp_catalogs": _mcp_catalog_summary(),
        "warnings": warnings,
    }


def _codex_runtime_status(root: Path) -> dict[str, Any]:
    codex_path = shutil.which("codex")
    if not codex_path:
        return {
            "ok": False,
            "binary": None,
            "active_version": "",
            "current_release": _safe_resolve(CODEX_STANDALONE_CURRENT),
            "app_server_processes": [],
            "stale_app_server_processes": [],
            "warnings": ["codex_binary_missing"],
        }

    binary = _safe_resolve(Path(codex_path))
    version_result = _run([codex_path, "--version"], cwd=root, timeout=4.0)
    active_version = _parse_codex_version(version_result["stdout"])
    processes = _codex_app_server_processes()
    stale = [
        process
        for process in processes
        if active_version and process.get("version") and process.get("version") != active_version
    ]
    warnings: list[str] = []
    if version_result["returncode"] != 0:
        warnings.append("codex_version_probe_failed")
    if stale:
        warnings.append("codex_app_server_stale")
    return {
        "ok": not warnings,
        "binary": binary,
        "active_version": active_version,
        "version_output": version_result["stdout"].strip()[:120],
        "current_release": _safe_resolve(CODEX_STANDALONE_CURRENT),
        "app_server_processes": processes,
        "stale_app_server_processes": stale,
        "restart_hint": "Restart Codex Desktop or the codex app-server when stale_app_server_processes is not empty.",
        "warnings": warnings,
    }


def _codex_app_server_processes() -> list[dict[str, Any]]:
    result = _run(["ps", "-eo", "pid=,ppid=,args="], cwd=PROJECT_ROOT, timeout=4.0)
    if result["returncode"] != 0:
        return []
    processes: list[dict[str, Any]] = []
    for line in result["stdout"].splitlines():
        stripped = line.strip()
        if "codex app-server" not in stripped or " app-server proxy " in stripped:
            continue
        parts = stripped.split(None, 2)
        if len(parts) < 3 or not parts[0].isdigit():
            continue
        pid = int(parts[0])
        exe_path = _proc_exe_path(pid)
        processes.append(
            {
                "pid": pid,
                "ppid": int(parts[1]) if parts[1].isdigit() else None,
                "exe": exe_path,
                "version": _version_from_release_path(exe_path),
                "cmd": parts[2][:240],
            }
        )
    return processes


def _proc_exe_path(pid: int) -> str:
    try:
        return os.readlink(f"/proc/{pid}/exe")
    except OSError:
        return ""


def _parse_codex_version(output: str) -> str:
    match = re.search(r"codex-cli\s+([0-9]+(?:\.[0-9]+){1,3})", output)
    return match.group(1) if match else ""


def _version_from_release_path(path: str) -> str:
    match = re.search(r"/releases/([0-9]+(?:\.[0-9]+){1,3})-", path)
    return match.group(1) if match else ""


def _safe_resolve(path: Path) -> str:
    try:
        return str(path.resolve())
    except OSError:
        return str(path)


def _runtime_readiness(root: Path, *, memory: ManagerMemoryStore) -> dict[str, Any]:
    manager_python = root / ".venv" / "bin" / "python"
    crm_python = CRM_ROOT / ".venv" / "bin" / "python"
    manager_venv = _venv_status(manager_python, cwd=root)
    crm_venv = _venv_status(crm_python, cwd=CRM_ROOT)
    browser_document_tools = _browser_document_tool_status()
    manifests = {
        "manager_pyproject": (root / "pyproject.toml").exists(),
        "manager_pre_commit_config": (root / ".pre-commit-config.yaml").exists(),
        "crm_requirements": (CRM_ROOT / "requirements.txt").exists(),
        "crm_requirements_runtime": (CRM_ROOT / "requirements-runtime.txt").exists(),
        "crm_requirements_dev": (CRM_ROOT / "requirements-dev.txt").exists(),
        "crm_pytest_ini": (CRM_ROOT / "pytest.ini").exists(),
        "manager_package_json": (root / "package.json").exists(),
        "manager_pnpm_lock": (root / "pnpm-lock.yaml").exists(),
        "crm_package_json": (CRM_ROOT / "package.json").exists(),
        "crm_pnpm_lock": (CRM_ROOT / "pnpm-lock.yaml").exists(),
    }
    env_files = {
        "manager": _env_file_status(root / ".env"),
        "crm": _env_file_status(CRM_ROOT / ".env"),
    }
    sqlite = {
        "ok": memory.path.exists() and os.access(memory.path.parent, os.W_OK),
        "path": str(memory.path),
        "exists": memory.path.exists(),
        "size_bytes": memory.path.stat().st_size if memory.path.exists() else 0,
        "parent_writable": os.access(memory.path.parent, os.W_OK),
    }
    warnings: list[str] = []
    if not manager_venv["ok"]:
        warnings.append("manager_venv_not_ready")
    if not crm_venv["ok"]:
        warnings.append("crm_venv_not_ready")
    if not browser_document_tools["ok"]:
        warnings.append("browser_document_tools_missing")
    if not sqlite["ok"]:
        warnings.append("manager_sqlite_not_ready")
    return {
        "ok": not warnings,
        "manager_venv": manager_venv,
        "crm_venv": crm_venv,
        "browser_document_tools": browser_document_tools,
        "package_manifests": manifests,
        "env_files": env_files,
        "sqlite": sqlite,
        "warnings": warnings,
    }


def _provider_readiness(providers: dict[str, Any]) -> dict[str, Any]:
    missing = [provider for provider in providers.get("providers") or [] if not bool(provider.get("configured"))]
    return {
        "ok": bool(providers.get("ok")),
        "matrix": providers.get("stage_matrix") or [],
        "provider_count": providers.get("provider_count", 0),
        "configured_count": providers.get("configured_count", 0),
        "external_access_backlog_count": len(missing),
        "external_access_backlog": [
            {
                "source_id": provider.get("source_id"),
                "stage": provider.get("stage"),
                "missing_env_names": provider.get("missing_env_names") or [],
                "manual_allowed": bool(provider.get("manual_allowed")),
            }
            for provider in missing
        ],
        "missing_env_by_provider": [
            {
                "source_id": provider.get("source_id"),
                "stage": provider.get("stage"),
                "configured": bool(provider.get("configured")),
                "missing_env_names": provider.get("missing_env_names") or [],
            }
            for provider in providers.get("providers") or []
        ],
        "safety": {
            "orders_blocked": True,
            "basket_blocked": True,
            "crm_writeback_blocked": True,
            "secrets_redacted": True,
        },
    }


def _read_catalog_counts(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"ok": False, "path": str(path), "warnings": ["missing_catalog"]}
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {"ok": False, "path": str(path), "warnings": ["invalid_catalog_json"]}
    if not isinstance(payload, dict):
        return {"ok": False, "path": str(path), "warnings": ["invalid_catalog_structure"]}
    all_tools = payload.get("expected_tool_names") or []
    return {
        "ok": True,
        "path": str(path),
        "tool_count": payload.get("expected_tool_count") or len(all_tools),
        "all_tools_count": len(all_tools),
        "version": payload.get("format"),
    }


def _git_state(root: Path) -> dict[str, Any]:
    branch_result = _run(["git", "branch", "--show-current"], cwd=root)
    status_result = _run(["git", "status", "--short"], cwd=root)
    status_lines = [line for line in status_result["stdout"].splitlines() if line.strip()]
    return {
        "ok": branch_result["returncode"] == 0 and status_result["returncode"] == 0,
        "branch": branch_result["stdout"].strip() or "unknown",
        "dirty": bool(status_lines),
        "modified_count": sum(1 for line in status_lines if not line.startswith("??")),
        "untracked_count": sum(1 for line in status_lines if line.startswith("??")),
        "status_short": status_lines[:80],
        "truncated": len(status_lines) > 80,
    }


def _tests_doctor_status(root: Path) -> dict[str, Any]:
    commands = [
        "python -m ruff check .",
        "python -m pytest -q -p no:cacheprovider",
        "python -m autostop_manager.cli doctor",
        "server/Unix only: bash scripts/doctor.sh --full",
    ]
    return {
        "status": "external",
        "commands": commands,
        "note": "Control report records the expected verification route; it does not run tests itself.",
        "doctor_script_exists": (root / "scripts" / "doctor.sh").exists(),
    }


def _production_health(root: Path, *, production_ops: dict[str, Any] | None = None) -> dict[str, Any]:
    compose_candidates = [
        root / "docker-compose.yml",
        root / "docker-compose.yaml",
        CRM_ROOT / "docker-compose.yml",
        CRM_ROOT / "docker-compose.yaml",
    ]
    compose_files = [str(path) for path in compose_candidates if path.exists()]
    ops = production_ops or {}
    watchdog = ops.get("watchdog") or {}
    watchdog_policy = watchdog.get("policy") or {}
    return {
        "mode": "read_only_summary",
        "compose_files": compose_files,
        "compose_config_present": bool(compose_files),
        "compose_config_ok": bool(((ops.get("compose") or {}).get("config") or {}).get("ok")),
        "nginx_config_ok": bool(((ops.get("nginx") or {}).get("config") or {}).get("ok")),
        "watchdog_timer_active": (watchdog.get("timer") or {}).get("active_state") == "active",
        "watchdog_policy_ok": bool(watchdog_policy.get("ok")),
        "watchdog_policy_state": watchdog_policy.get("state", "unknown"),
        "container_health": ((ops.get("container") or {}).get("autostopcrm") or {}).get("health", "unknown"),
        "notes": [
            "Production checks remain read-only in ControlReportV1.",
            "The production watchdog policy requires both legacy systemd units to be absent.",
            "Run docker compose health/smoke separately before any production-changing action.",
        ],
    }


def _production_ops(root: Path) -> dict[str, Any]:
    compose_path = _first_existing([CRM_ROOT / "docker-compose.yml", CRM_ROOT / "docker-compose.yaml"])
    compose_config: dict[str, Any]
    compose_ps: dict[str, Any]
    if compose_path:
        compose_config_result = _run(
            ["docker", "compose", "-f", str(compose_path), "config", "--quiet"], cwd=CRM_ROOT, timeout=8.0
        )
        compose_ps_result = _run(["docker", "compose", "-f", str(compose_path), "ps"], cwd=CRM_ROOT, timeout=8.0)
        compose_config = {
            "ok": compose_config_result["returncode"] == 0,
            "returncode": compose_config_result["returncode"],
            "stderr": compose_config_result["stderr"][:400],
        }
        compose_ps = {
            "ok": compose_ps_result["returncode"] == 0,
            "returncode": compose_ps_result["returncode"],
            "lines": compose_ps_result["stdout"].splitlines()[:20],
        }
    else:
        compose_config = {"ok": False, "error": "compose_file_missing"}
        compose_ps = {"ok": False, "error": "compose_file_missing", "lines": []}

    nginx_result = _run(["nginx", "-t"], cwd=root, timeout=8.0)
    container = _container_status("autostopcrm", cwd=root)
    watchdog_timer = _systemd_unit_status("autostopcrm-watchdog.timer", cwd=root)
    watchdog_service = _systemd_unit_status("autostopcrm-watchdog.service", cwd=root)
    watchdog = {
        "timer": watchdog_timer,
        "service": watchdog_service,
        "policy": _watchdog_policy_status(timer=watchdog_timer, service=watchdog_service),
    }
    safe_operation_gates = [
        {
            "operation": "nginx_reload",
            "allowed_command": "nginx -t && systemctl reload nginx",
            "required_gates": ["dirty state recorded", "nginx config ok", "public HTTPS smoke read-back"],
        },
        {
            "operation": "docker_compose_up_or_restart",
            "allowed_command": "docker compose config && docker compose up -d",
            "required_gates": [
                "dirty state recorded",
                "env file presence checked",
                "compose config ok",
                "backup/read-back noted",
                "smoke baseline captured",
            ],
        },
    ]
    ok = (
        bool(compose_path)
        and compose_config.get("ok")
        and nginx_result["returncode"] == 0
        and container.get("state") == "running"
        and container.get("health") in {"healthy", "none", "unknown"}
        and watchdog["policy"].get("ok")
    )
    warnings: list[str] = []
    if not compose_config.get("ok"):
        warnings.append("compose_config_not_ok")
    if nginx_result["returncode"] != 0:
        warnings.append("nginx_config_not_ok")
    if container.get("state") != "running":
        warnings.append("autostopcrm_container_not_running")
    if watchdog["policy"].get("active_units"):
        warnings.append("autostopcrm_watchdog_unit_active")
    if watchdog["policy"].get("installed_units"):
        warnings.append("autostopcrm_watchdog_legacy_units_present")
    if watchdog["policy"].get("state") == "unknown":
        warnings.append("autostopcrm_watchdog_policy_unknown")
    return {
        "ok": bool(ok),
        "mode": "read_only_ops_readiness",
        "compose": {
            "path": str(compose_path) if compose_path else None,
            "config": compose_config,
            "ps": compose_ps,
        },
        "nginx": {
            "config": {
                "ok": nginx_result["returncode"] == 0,
                "returncode": nginx_result["returncode"],
                "stderr": nginx_result["stderr"][:400],
            }
        },
        "watchdog": watchdog,
        "container": {"autostopcrm": container},
        "read_only_smoke_commands": [
            "curl -fsS https://crm.autostopcrm.ru/",
            "curl -fsS -X POST https://crm.autostopcrm.ru/mcp initialize",
            "curl -fsS http://127.0.0.1:8000/api/get_board_context",
            "docker exec autostopcrm python scripts/container_healthcheck.py",
        ],
        "safe_operation_gates": safe_operation_gates,
        "forbidden_without_explicit_owner_command": [
            "CRM writes",
            "supplier orders",
            "supplier basket actions",
            "email sends",
            "destructive cleanup",
            "watchdog installation or enablement",
        ],
        "warnings": warnings,
    }


def _core_tools() -> dict[str, Any]:
    tools = {name: _tool_status(name, commands) for name, commands in CORE_TOOL_COMMANDS.items()}
    required_present = [name for name in REQUIRED_CORE_TOOLS if tools.get(name, {}).get("present")]
    missing_required = sorted(REQUIRED_CORE_TOOLS.difference(required_present))
    return {
        "ok": not missing_required,
        "required_present_count": len(required_present),
        "required_missing": missing_required,
        "tools": tools,
    }


def _tool_status(name: str, commands: list[list[str]]) -> dict[str, Any]:
    attempted: list[str] = []
    for command in commands:
        executable = command[0]
        if not shutil.which(executable):
            attempted.append(" ".join(command))
            continue
        result = _run(command, cwd=PROJECT_ROOT, timeout=4.0)
        output = (result["stdout"] or result["stderr"]).strip().splitlines()
        return {
            "present": True,
            "command": " ".join(command),
            "version": output[0][:180] if output else "",
            "returncode": result["returncode"],
        }
    return {
        "present": False,
        "command": attempted[0] if attempted else name,
        "version": "",
        "returncode": 127,
    }


def _tmp_writable() -> bool:
    try:
        with tempfile.TemporaryFile(mode="w", encoding="utf-8", dir="/tmp") as handle:
            handle.write("ok")
    except OSError:
        return False
    else:
        return True


def _os_pretty_name() -> str:
    os_release = Path("/etc/os-release")
    if not os_release.exists():
        return platform.platform()
    for line in os_release.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("PRETTY_NAME="):
            return line.split("=", 1)[1].strip().strip('"')
    return platform.platform()


def _disk_summary(paths: list[Path]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in paths:
        if not path.exists():
            rows.append({"path": str(path), "ok": False, "error": "missing"})
            continue
        usage = shutil.disk_usage(path)
        used = usage.total - usage.free
        rows.append(
            {
                "path": str(path),
                "ok": True,
                "total_bytes": usage.total,
                "used_bytes": used,
                "free_bytes": usage.free,
                "used_percent": round((used / usage.total) * 100, 2) if usage.total else 0,
            }
        )
    return rows


def _proc_memory_summary() -> dict[str, Any]:
    path = Path("/proc/meminfo")
    if not path.exists():
        return {"ok": False, "error": "meminfo_missing"}
    values: dict[str, int] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if ":" not in line:
            continue
        name, raw_value = line.split(":", 1)
        parts = raw_value.strip().split()
        if parts and parts[0].isdigit():
            values[name] = int(parts[0]) * 1024
    total = values.get("MemTotal", 0)
    available = values.get("MemAvailable", 0)
    return {
        "ok": total > 0,
        "total_bytes": total,
        "available_bytes": available,
        "used_percent": round(((total - available) / total) * 100, 2) if total else 0,
    }


def _classify_ports(ports: dict[str, Any]) -> dict[str, Any]:
    classifications = []
    for listener in ports.get("public_listeners") or []:
        local_address = str(listener.get("local_address") or "")
        line = str(listener.get("line") or "")
        port = _extract_port(local_address)
        service = {
            "22": "ssh",
            "80": "http",
            "443": "https",
            "8080": "http_alt",
            "47895": "amneziawg",
            "10443": "telegram_relay",
        }.get(port, "unknown")
        if port in {"80", "443"}:
            risk = "expected_public_web"
        elif port == "22":
            risk = "expected_admin_ssh"
        elif port == "47895":
            risk = "expected_public_vpn"
        elif port == "10443":
            risk = "expected_vpn_telegram_relay"
        elif port == "2525" and local_address.startswith("172."):
            service = "smtp_relay_docker_bridge"
            risk = "expected_docker_bridge_relay"
        elif line.startswith("udp ") and '("codex"' in line:
            service = "codex_runtime_udp"
            risk = "expected_codex_runtime_socket"
        elif line.startswith("udp ") and "users:" not in line:
            service = "transient_udp_socket"
            risk = "expected_transient_udp_socket"
        elif port in {"8000", "8001"}:
            risk = "review_crm_internal_port_public"
        elif port == "8080":
            risk = "expected_public_http_alt"
        else:
            risk = "review_public_listener"
        classifications.append(
            {
                "port": port,
                "service": service,
                "risk": risk,
                "local_address": local_address,
            }
        )
    review_count = sum(1 for item in classifications if str(item["risk"]).startswith("review_"))
    return {
        "ok": ports.get("ok", False),
        "public_count": len(ports.get("public_listeners") or []),
        "local_count": len(ports.get("local_listeners") or []),
        "review_public_count": review_count,
        "classifications": classifications,
    }


def _extract_port(local_address: str) -> str:
    if "]:" in local_address:
        return local_address.rsplit("]:", 1)[-1]
    if ":" in local_address:
        return local_address.rsplit(":", 1)[-1]
    return local_address


def _pre_commit_hook_status(repo: Path) -> dict[str, Any]:
    hook = repo / ".git" / "hooks" / "pre-commit"
    config = repo / ".pre-commit-config.yaml"
    return {
        "repo": str(repo),
        "config_present": config.exists(),
        "hook_present": hook.exists(),
        "hook_installed": hook.exists() and os.access(hook, os.X_OK),
    }


def _mcp_import_status(python_path: Path, *, cwd: Path) -> dict[str, Any]:
    if not python_path.exists():
        return {"ok": False, "python": str(python_path), "error": "python_missing"}
    result = _run(
        [str(python_path), "-c", "from mcp.server.fastmcp import FastMCP; print('ok')"],
        cwd=cwd,
        timeout=6.0,
    )
    return {
        "ok": result["returncode"] == 0,
        "python": str(python_path),
        "returncode": result["returncode"],
        "stderr": result["stderr"][:240],
    }


def _safe_path_inventory(
    root: Path,
    pattern: str | None = None,
    *,
    directories_only: bool = False,
) -> tuple[list[Path], bool]:
    try:
        if not root.exists():
            return [], False
        candidates = root.glob(pattern) if pattern else root.iterdir()
        paths = [path for path in candidates if not directories_only or path.is_dir()]
    except OSError:
        return [], False
    return sorted(paths), True


def _codex_skill_inventory() -> dict[str, Any]:
    system_skills, system_skills_readable = _safe_path_inventory(CODEX_SYSTEM_SKILLS_ROOT, "*/SKILL.md")
    plugin_skills, plugin_skills_readable = _safe_path_inventory(
        CODEX_PLUGIN_CACHE_ROOT,
        "*/**/skills/*/SKILL.md",
    )
    plugin_dirs, plugin_dirs_readable = _safe_path_inventory(CODEX_PLUGIN_CACHE_ROOT, directories_only=True)
    plugins = [path.name for path in plugin_dirs]
    return {
        "system_skills_root": str(CODEX_SYSTEM_SKILLS_ROOT),
        "plugin_cache_root": str(CODEX_PLUGIN_CACHE_ROOT),
        "system_skills_readable": system_skills_readable,
        "plugin_cache_readable": plugin_skills_readable and plugin_dirs_readable,
        "system_skill_count": len(system_skills),
        "plugin_skill_count": len(plugin_skills),
        "plugin_count": len(plugins),
        "plugins": plugins,
        "system_skills": [path.parent.name for path in system_skills],
    }


def _venv_status(python_path: Path, *, cwd: Path) -> dict[str, Any]:
    package_versions = _package_versions(python_path, cwd=cwd)
    required = ["mcp", "pytest", "ruff"]
    missing_required = [name for name in required if not package_versions.get("packages", {}).get(name)]
    return {
        "ok": python_path.exists() and package_versions.get("ok") and not missing_required,
        "python": str(python_path),
        "python_exists": python_path.exists(),
        "packages": package_versions.get("packages") or {},
        "missing_required_packages": missing_required,
        "warnings": package_versions.get("warnings") or [],
    }


def _package_versions(python_path: Path, *, cwd: Path) -> dict[str, Any]:
    if not python_path.exists():
        return {"ok": False, "packages": {}, "warnings": ["python_missing"]}
    script = (
        "import importlib.metadata as md, json; "
        f"pkgs={VENV_PACKAGES!r}; "
        "out={}; "
        "[(out.__setitem__(p, md.version(p)) if True else None) for p in []]; "
        "\nfor p in pkgs:\n"
        "    try:\n"
        "        out[p] = md.version(p)\n"
        "    except md.PackageNotFoundError:\n"
        "        out[p] = None\n"
        "print(json.dumps(out, sort_keys=True))"
    )
    result = _run([str(python_path), "-c", script], cwd=cwd, timeout=8.0)
    if result["returncode"] != 0:
        return {"ok": False, "packages": {}, "warnings": ["package_probe_failed"]}
    try:
        packages = json.loads(result["stdout"])
    except json.JSONDecodeError:
        return {"ok": False, "packages": {}, "warnings": ["package_probe_invalid_json"]}
    return {"ok": True, "packages": packages, "warnings": []}


def _browser_document_tool_status() -> dict[str, Any]:
    tools = _core_tools().get("tools") or {}
    required = ["chromium", "pdftotext", "pdftoppm", "ghostscript", "libreoffice", "7z"]
    missing = [name for name in required if not (tools.get(name) or {}).get("present")]
    return {
        "ok": not missing,
        "required": required,
        "missing": missing,
        "tools": {name: tools.get(name) for name in required},
    }


def _env_file_status(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"present": False, "path": str(path), "key_count": 0, "key_names": [], "mode": None}
    key_names: list[str] = []
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key = stripped.split("=", 1)[0].strip()
            if key:
                key_names.append(key)
    except OSError:
        key_names = []
    mode = oct(path.stat().st_mode & 0o777)
    return {
        "present": True,
        "path": str(path),
        "key_count": len(sorted(set(key_names))),
        "key_names": sorted(set(key_names)),
        "mode": mode,
    }


def _first_existing(paths: list[Path]) -> Path | None:
    for path in paths:
        if path.exists():
            return path
    return None


def _systemd_unit_status(unit: str, *, cwd: Path) -> dict[str, Any]:
    if not shutil.which("systemctl"):
        return {"ok": False, "unit": unit, "error": "systemctl_missing"}
    result = _run(
        ["systemctl", "show", unit, "--property=LoadState,ActiveState,SubState,UnitFileState", "--no-pager"],
        cwd=cwd,
        timeout=5.0,
    )
    fields: dict[str, str] = {}
    for line in result["stdout"].splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            fields[key] = value
    return {
        "ok": result["returncode"] == 0 and fields.get("LoadState") == "loaded",
        "unit": unit,
        "load_state": fields.get("LoadState", "unknown"),
        "active_state": fields.get("ActiveState", "unknown"),
        "sub_state": fields.get("SubState", "unknown"),
        "unit_file_state": fields.get("UnitFileState", "unknown"),
    }


def _watchdog_policy_status(*, timer: dict[str, Any], service: dict[str, Any]) -> dict[str, Any]:
    units = {"timer": timer, "service": service}
    installed_units = sorted(name for name, status in units.items() if status.get("load_state") == "loaded")
    active_units = sorted(name for name, status in units.items() if status.get("active_state") == "active")
    absent_units = sorted(name for name, status in units.items() if status.get("load_state") == "not-found")

    policy_ok = len(absent_units) == len(units)
    if policy_ok:
        state = "absent"
    elif active_units:
        state = "active"
    elif installed_units:
        state = "legacy_units_present"
    else:
        state = "unknown"

    return {
        "ok": policy_ok,
        "desired_state": "absent",
        "state": state,
        "absent_units": absent_units,
        "installed_units": installed_units,
        "active_units": active_units,
    }


def _container_status(name: str, *, cwd: Path) -> dict[str, Any]:
    if not shutil.which("docker"):
        return {"ok": False, "name": name, "state": "unknown", "health": "unknown", "error": "docker_missing"}
    result = _run(
        [
            "docker",
            "inspect",
            "--format",
            "{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}",
            name,
        ],
        cwd=cwd,
        timeout=5.0,
    )
    if result["returncode"] != 0:
        return {"ok": False, "name": name, "state": "missing", "health": "unknown"}
    parts = result["stdout"].strip().split()
    state = parts[0] if parts else "unknown"
    health = parts[1] if len(parts) > 1 else "unknown"
    return {"ok": state == "running", "name": name, "state": state, "health": health}


def _open_risk_score(**sections: dict[str, Any]) -> dict[str, Any]:
    score = 0
    items: list[dict[str, Any]] = []

    def add(points: int, category: str, reason: str) -> None:
        nonlocal score
        score += points
        items.append({"points": points, "category": category, "reason": reason})

    if sections["git"].get("dirty"):
        add(15, "git", "working tree is dirty")
    if not sections["providers"].get("ok"):
        add(15, "providers", "provider catalog status could not be built")
    if not sections["server_environment"].get("ok"):
        add(20, "server_environment", "required server tools or /tmp readiness missing")
    port_review_count = ((sections["server_environment"].get("ports") or {}).get("review_public_count")) or 0
    if port_review_count:
        add(min(20, port_review_count * 8), "ports", "public listeners need review")
    if not sections["codex_readiness"].get("ok"):
        add(15, "codex_readiness", "hooks, skills, or MCP imports need attention")
    if not sections["runtime_readiness"].get("ok"):
        add(15, "runtime_readiness", "venv, package, browser, document, or SQLite readiness needs attention")
    if not sections["production_ops"].get("ok"):
        add(15, "production_ops", "production compose/nginx/watchdog/container readiness needs attention")

    bounded = min(score, 100)
    level = "green"
    if bounded >= 70:
        level = "red"
    elif bounded > 0:
        level = "yellow"
    return {"score": bounded, "level": level, "items": items}


def _public_ports() -> dict[str, Any]:
    result = _run(["ss", "-ltnup", "-H"], cwd=PROJECT_ROOT)
    if result["returncode"] != 0:
        return {"ok": False, "error": "ss unavailable", "public_listeners": [], "local_listeners": []}
    public_listeners = []
    local_listeners = []
    for line in result["stdout"].splitlines():
        redacted = _redact_text(line)
        parts = redacted.split()
        local = parts[4] if len(parts) > 4 else redacted
        listener = {"line": redacted[:240], "local_address": local}
        if local.startswith(("127.", "[::1]", "::1")):
            local_listeners.append(listener)
        else:
            public_listeners.append(listener)
    return {
        "ok": True,
        "public_listeners": public_listeners[:40],
        "local_listeners": local_listeners[:40],
        "truncated": len(public_listeners) > 40 or len(local_listeners) > 40,
    }


def _collect_risks(**sections: dict[str, Any]) -> list[dict[str, str]]:
    risks: list[dict[str, str]] = []
    system_audit = sections["system_audit"]
    if not system_audit.get("ok"):
        risks.append(
            {"severity": "yellow", "category": "system", "message": "system audit has warnings or failing checks"}
        )
    server_environment = sections["server_environment"]
    if not server_environment.get("ok"):
        risks.append(
            {
                "severity": "yellow",
                "category": "server_environment",
                "message": "required server tools or /tmp readiness are incomplete",
            }
        )
    codex_readiness = sections["codex_readiness"]
    if not codex_readiness.get("ok"):
        risks.append(
            {"severity": "yellow", "category": "codex", "message": "Codex skills, hooks, or MCP imports need attention"}
        )
    runtime_readiness = sections["runtime_readiness"]
    if not runtime_readiness.get("ok"):
        risks.append(
            {
                "severity": "yellow",
                "category": "runtime",
                "message": "Manager/CRM venv, package, document, browser, or SQLite readiness is incomplete",
            }
        )
    git = sections["git"]
    if git.get("dirty"):
        risks.append(
            {
                "severity": "yellow",
                "category": "git",
                "message": "working tree is dirty; preserve user changes before production actions",
            }
        )
    knowledge = sections["knowledge"]
    if not knowledge.get("ok"):
        risks.append(
            {"severity": "yellow", "category": "knowledge", "message": "knowledge or annotations audit has warnings"}
        )
    memory = sections["memory"]
    if memory.get("duplicate_groups") or memory.get("expired_count") or memory.get("superseded_count"):
        risks.append(
            {
                "severity": "yellow",
                "category": "memory",
                "message": "memory review has duplicate, expired, or superseded candidates",
            }
        )
    mcp = sections["mcp"]
    if not mcp.get("manager", {}).get("ok") or not mcp.get("crm", {}).get("ok"):
        risks.append(
            {"severity": "yellow", "category": "mcp", "message": "one or more MCP catalog files are missing or invalid"}
        )
    providers = sections["providers"]
    if not providers.get("ok"):
        risks.append(
            {
                "severity": "yellow",
                "category": "providers",
                "message": "provider catalog status could not be built",
            }
        )
    production = sections["production"]
    if not production.get("compose_config_present"):
        risks.append(
            {
                "severity": "yellow",
                "category": "production",
                "message": "docker compose config was not found in expected locations",
            }
        )
    production_ops = sections["production_ops"]
    if not production_ops.get("ok"):
        risks.append(
            {
                "severity": "yellow",
                "category": "production_ops",
                "message": "production compose/nginx/watchdog/container gates are not all green",
            }
        )
    ports = sections["ports"]
    if not ports.get("ok"):
        risks.append({"severity": "yellow", "category": "ports", "message": "public port inspection failed"})
    open_risk = sections["open_risk"]
    if open_risk.get("level") == "red":
        risks.append({"severity": "red", "category": "open_risk", "message": "open risk score is red"})
    elif ((server_environment.get("ports") or {}).get("review_public_count")) or 0:
        risks.append(
            {"severity": "yellow", "category": "ports", "message": "one or more public listeners should be reviewed"}
        )
    return risks


def _traffic_status(risks: list[dict[str, str]]) -> str:
    if any(risk["severity"] == "red" for risk in risks):
        return "red"
    if risks:
        return "yellow"
    return "green"


def _run(command: list[str], *, cwd: Path, timeout: float = 5.0) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"returncode": 127, "stdout": "", "stderr": str(exc)}
    return {
        "returncode": completed.returncode,
        "stdout": _redact_text(completed.stdout),
        "stderr": _redact_text(completed.stderr),
    }


def _redact_payload(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _redact_payload(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_payload(item) for item in value]
    if isinstance(value, str):
        return _redact_text(value)
    return value


def _redact_text(value: str) -> str:
    redacted = value
    for pattern in SECRET_PATTERNS:
        redacted = pattern.sub(
            lambda match: f"{match.group(1)}***" if match.lastindex else "***",
            redacted,
        )
    return redacted
