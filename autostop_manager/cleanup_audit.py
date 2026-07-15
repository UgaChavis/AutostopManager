from __future__ import annotations

import json
import sqlite3
import subprocess
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import PROJECT_ROOT
from .storage import ManagerMemoryStore, _now, _string_list

PROTECTED_AGENT_DOCS = {
    "crm_mcp_catalog.json",
    "knowledge_annotations.jsonl",
    "knowledge_map.json",
    "knowledge_shelves.md",
    "manager_mcp_catalog.json",
}
IGNORED_CACHE_SCAN_ROOTS = {
    ".codex-remote-attachments",
    ".git",
    ".venv",
    "data",
    "output",
}


@dataclass(frozen=True)
class CleanupCandidate:
    category: str
    path: str
    size_bytes: int
    risk: str
    recommended_action: str
    requires_approval: bool
    matched_by: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "path": self.path,
            "size_bytes": self.size_bytes,
            "risk": self.risk,
            "recommended_action": self.recommended_action,
            "requires_approval": self.requires_approval,
            "matched_by": self.matched_by,
        }


def build_cleanup_audit(
    *,
    project_root: Path | str = PROJECT_ROOT,
    store: ManagerMemoryStore | None = None,
) -> dict[str, Any]:
    root = Path(project_root)
    memory = store or ManagerMemoryStore()
    candidates: list[CleanupCandidate] = []
    retained_items: list[CleanupCandidate] = []
    candidates.extend(_ignored_cache_candidates(root))
    candidates.extend(_untracked_generated_artifact_candidates(root))
    candidates.extend(_workspace_output_tree_candidates(root))
    candidates.extend(_tracked_pdf_duplicate_candidates(root))
    candidates.extend(_unreferenced_agent_doc_candidates(root))
    local_db = _local_db_candidate(memory)
    if local_db is not None:
        retained_items.append(local_db)
    candidates.extend(_source_pack_overindexed_candidates(root))

    category_counts = Counter(candidate.category for candidate in candidates)
    retained_category_counts = Counter(item.category for item in retained_items)
    total_size = sum(candidate.size_bytes for candidate in candidates)
    retained_size = sum(item.size_bytes for item in retained_items)
    return {
        "ok": True,
        "mode": "dry_run",
        "summary": {
            "candidate_count": len(candidates),
            "category_counts": dict(sorted(category_counts.items())),
            "total_size_bytes": total_size,
            "requires_approval_count": sum(1 for candidate in candidates if candidate.requires_approval),
            "retained_count": len(retained_items),
            "retained_category_counts": dict(sorted(retained_category_counts.items())),
            "retained_size_bytes": retained_size,
        },
        "candidates": [candidate.to_dict() for candidate in candidates],
        "retained_items": [item.to_dict() for item in retained_items],
        "checked_at": _now(),
    }


def _ignored_cache_candidates(root: Path) -> list[CleanupCandidate]:
    candidates: list[CleanupCandidate] = []
    project_bytecode_caches = [
        cache_path
        for cache_path in sorted(root.rglob("__pycache__"))
        if not _is_under_ignored_cache_root(cache_path, root)
    ]
    for cache_path in [root / ".pytest_cache", root / ".ruff_cache", *project_bytecode_caches]:
        if not cache_path.exists():
            continue
        candidates.append(
            CleanupCandidate(
                category="ignored_cache",
                path=_display_path(cache_path, root),
                size_bytes=_path_size(cache_path),
                risk="low",
                recommended_action="delete_after_approval",
                requires_approval=True,
                matched_by="ignored cache directory",
            )
        )
    return candidates


def _is_under_ignored_cache_root(path: Path, root: Path) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return True
    return bool(relative.parts and relative.parts[0] in IGNORED_CACHE_SCAN_ROOTS)


def _untracked_generated_artifact_candidates(root: Path) -> list[CleanupCandidate]:
    candidates: list[CleanupCandidate] = []
    for relative_path in _git_untracked_paths(root):
        path = root / relative_path
        normalized = relative_path.replace("\\", "/")
        if "/" in normalized:
            continue
        if path.suffix.lower() not in {".html", ".pdf"}:
            continue
        name = path.name.lower()
        generated_name_markers = (
            "autostopcrm-",
            "egrul-",
            "invoice",
            "repair-order",
            "заказ-наряд",
            "счет",
            "счёт",
            "акт",
            "кп",
        )
        if not any(name.startswith(marker) for marker in generated_name_markers):
            continue
        candidates.append(
            CleanupCandidate(
                category="untracked_generated_artifact",
                path=normalized,
                size_bytes=_path_size(path),
                risk="low",
                recommended_action="delete",
                requires_approval=True,
                matched_by="untracked generated business document at project root",
            )
        )
    return candidates


def _workspace_output_tree_candidates(root: Path) -> list[CleanupCandidate]:
    candidates: list[CleanupCandidate] = []
    for relative_path in ("out", "reports", "tmp", "data/backups"):
        path = root / relative_path
        if not path.exists():
            continue
        size_bytes = _path_size(path)
        if size_bytes <= 0:
            continue
        candidates.append(
            CleanupCandidate(
                category="generated_workspace_artifact",
                path=relative_path,
                size_bytes=size_bytes,
                risk="low",
                recommended_action="delete",
                requires_approval=True,
                matched_by="generated output tree",
            )
        )
    return candidates


def _tracked_pdf_duplicate_candidates(root: Path) -> list[CleanupCandidate]:
    source_cache = root / "docs" / "agent" / "automotive_sources" / "source_cache"
    if not source_cache.exists():
        return []
    candidates: list[CleanupCandidate] = []
    for pdf_path in sorted(source_cache.rglob("*.pdf")):
        pack_root = _source_pack_root(source_cache, pdf_path)
        if _has_text_equivalent(pack_root, pdf_path):
            candidates.append(
                CleanupCandidate(
                    category="tracked_pdf_duplicate",
                    path=_display_path(pdf_path, root),
                    size_bytes=_path_size(pdf_path),
                    risk="medium",
                    recommended_action="keep_text_equivalent",
                    requires_approval=True,
                    matched_by="source_cache PDF with Markdown or JSONL equivalent",
                )
            )
    return candidates


def _unreferenced_agent_doc_candidates(root: Path) -> list[CleanupCandidate]:
    docs_agent = root / "docs" / "agent"
    if not docs_agent.exists():
        return []
    referenced = _referenced_agent_paths(root)
    candidates: list[CleanupCandidate] = []
    for path in sorted(docs_agent.rglob("*")):
        if not path.is_file() or "source_cache" in path.parts:
            continue
        relative = _relative_posix(path, root)
        if not relative.startswith("docs/agent/"):
            continue
        if path.name in PROTECTED_AGENT_DOCS or relative in referenced:
            continue
        candidates.append(
            CleanupCandidate(
                category="unreferenced_agent_doc",
                path=relative,
                size_bytes=_path_size(path),
                risk="medium",
                recommended_action="link_to_knowledge_map",
                requires_approval=True,
                matched_by="not referenced by knowledge_map or knowledge_annotations",
            )
        )
    return candidates


def _local_db_candidate(store: ManagerMemoryStore) -> CleanupCandidate | None:
    db_path = store.path
    if not db_path.exists():
        return None
    table_counts = _sqlite_table_counts(db_path)
    matched_by = "local SQLite database"
    if table_counts:
        counts = ", ".join(f"{name}={count}" for name, count in sorted(table_counts.items()))
        matched_by = f"{matched_by}; {counts}"
    return CleanupCandidate(
        category="local_db",
        path=str(db_path),
        size_bytes=_path_size(db_path),
        risk="high",
        recommended_action="keep",
        requires_approval=True,
        matched_by=matched_by,
    )


def _source_pack_overindexed_candidates(root: Path) -> list[CleanupCandidate]:
    map_path = root / "docs" / "agent" / "knowledge_map.json"
    if not map_path.exists():
        return []
    try:
        payload = json.loads(map_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return []
    if not isinstance(payload, dict):
        return []
    candidates: list[CleanupCandidate] = []
    domains = payload.get("domains")
    if not isinstance(domains, dict):
        return []
    for domain, route in domains.items():
        if not isinstance(route, dict):
            continue
        primary_files = _string_list(route.get("primary_files"))
        source_pack_files = [item for item in primary_files if "source_cache" in item.replace("\\", "/")]
        if len(primary_files) >= 25 or len(source_pack_files) >= 20:
            candidates.append(
                CleanupCandidate(
                    category="source_pack_overindexed",
                    path=f"knowledge_map:{domain}",
                    size_bytes=0,
                    risk="low",
                    recommended_action="link_to_knowledge_map",
                    requires_approval=True,
                    matched_by=f"{len(primary_files)} primary files, {len(source_pack_files)} source_cache files",
                )
            )
    return candidates


def _referenced_agent_paths(root: Path) -> set[str]:
    referenced: set[str] = set()
    map_path = root / "docs" / "agent" / "knowledge_map.json"
    if map_path.exists():
        try:
            payload = json.loads(map_path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        domains = payload.get("domains")
        if not isinstance(domains, dict):
            domains = {}
        for route in domains.values():
            if not isinstance(route, dict):
                continue
            for key in ["source_of_truth_files", "primary_files", "reference_files", "optional_runtime_files"]:
                for raw_path in _string_list(route.get(key)):
                    text = str(raw_path).replace("\\", "/")
                    if text.startswith("docs/agent/"):
                        referenced.add(text)
    annotations_path = root / "docs" / "agent" / "knowledge_annotations.jsonl"
    if annotations_path.exists():
        try:
            annotations_content = annotations_path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError):
            annotations_content = ""
        for raw_line in annotations_content.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            raw_path = str(payload.get("path") or "").replace("\\", "/")
            if raw_path.startswith("docs/agent/"):
                referenced.add(raw_path)
    return referenced


def _git_untracked_paths(root: Path) -> list[str]:
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), "ls-files", "--others", "--exclude-standard"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return []
    return [line.strip() for line in completed.stdout.splitlines() if line.strip()]


def _source_pack_root(source_cache: Path, path: Path) -> Path:
    try:
        relative = path.relative_to(source_cache)
    except ValueError:
        return path.parent
    first = relative.parts[0] if relative.parts else ""
    return source_cache / first if first else path.parent


def _has_text_equivalent(pack_root: Path, pdf_path: Path) -> bool:
    stem = pdf_path.stem
    return any(candidate.stem == stem for candidate in pack_root.rglob("*.md")) or any(
        candidate.stem == stem for candidate in pack_root.rglob("*.jsonl")
    )


def _sqlite_table_counts(db_path: Path) -> dict[str, int]:
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error:
        return {}
    try:
        rows = conn.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table'
            AND name IN (
                'manager_rules',
                'knowledge_documents',
                'knowledge_sections',
                'knowledge_annotations',
                'notes',
                'facts',
                'tasks',
                'journal'
            )
            """
        ).fetchall()
    except sqlite3.Error:
        return {}
    else:
        counts: dict[str, int] = {}
        for row in rows:
            name = str(row["name"])
            counts[name] = int(conn.execute(f"SELECT COUNT(*) AS count FROM {name}").fetchone()["count"] or 0)
        return counts
    finally:
        conn.close()


def _path_size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    if not path.exists():
        return 0
    total = 0
    for child in path.rglob("*"):
        if child.is_file():
            total += child.stat().st_size
    return total


def _display_path(path: Path, root: Path) -> str:
    try:
        return _relative_posix(path, root)
    except ValueError:
        return str(path)


def _relative_posix(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()
