from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .config import PROJECT_ROOT
from .knowledge_base import KNOWLEDGE_MAP_PATH
from .storage import _now, _string_list


TEXT_EXTENSIONS = {".md", ".txt", ".json", ".jsonl", ".csv", ".yaml", ".yml", ".py", ".html", ".css", ".js"}
DOCUMENT_EXTENSIONS = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ods", ".ppt", ".pptx"}
PRIVATE_PATH_MARKERS = {"private_knowledge", "credentials", "secrets", "cashbox", "generated_invoices", "attachments"}
RAW_DATA_MARKERS = {"crm", "client", "customer", "email", "mail", "invoice", "payment", "cashbox", "repair_order"}
UNSAFE_METADATA_FLAGS = {"outside_project", "private_path", "raw_crm_email_or_finance_risk", "secret_risk"}
TERM_PATTERN = re.compile(r"[0-9a-zа-яё]+", re.IGNORECASE)

_DOMAIN_LIST_FIELDS = (
    "use_when",
    "aliases",
    "keywords",
    "questions",
    "source_of_truth_files",
    "primary_files",
    "reference_files",
    "required_context",
)


def build_knowledge_intake_plan(
    source_path: str | Path,
    *,
    apply: bool = False,
    project_root: Path | str = PROJECT_ROOT,
) -> dict[str, Any]:
    root = Path(project_root).resolve()
    path = Path(source_path)
    resolved = path if path.is_absolute() else root / path
    resolved = resolved.resolve()
    exists = resolved.exists()
    safety_flags = _safety_flags(resolved, root=root, exists=exists)
    path_text = _safe_path_text(resolved, root=root, safety_flags=safety_flags)
    domain = _classify_domain(path_text, _sample_text(resolved, safety_flags=safety_flags))
    source_type = _source_type(resolved)
    target_updates = _target_updates(domain, source_type, path_text, safety_flags=safety_flags)
    apply_allowed = exists and not any(flag in safety_flags for flag in UNSAFE_METADATA_FLAGS)
    return {
        "ok": apply_allowed or not apply,
        "schema": "KnowledgeIntakeDraft",
        "generated_at": _now(),
        "source_path": path_text,
        "exists": exists,
        "domain": domain,
        "classification": {
            "source_type": source_type,
            "extension": resolved.suffix.casefold(),
            "is_text_like": resolved.suffix.casefold() in TEXT_EXTENSIONS,
            "is_document_like": resolved.suffix.casefold() in DOCUMENT_EXTENSIONS,
            "review_gate": "required",
        },
        "durable_rules": _durable_rule_suggestions(domain, source_type, safety_flags),
        "target_updates": target_updates,
        "safety_flags": safety_flags,
        "apply_allowed": apply_allowed,
        "apply_requested": apply,
        "apply_result": _apply_result(apply=apply, apply_allowed=apply_allowed),
        "required_follow_up": [
            "knowledge-sync",
            "knowledge-audit",
            "annotations-audit",
        ],
        "privacy": {
            "raw_private_file_committed": False,
            "raw_crm_email_secret_data_persisted": False,
            "content_preview_included": False,
        },
    }


def _classify_domain(path_text: str, sample: str) -> str:
    lower_path = path_text.casefold()
    if "knowledge_map.json" in lower_path or "knowledge_annotations.jsonl" in lower_path:
        return "knowledge_intake"
    haystack = f"{path_text}\n{sample}".casefold()
    haystack_terms = _terms(haystack)
    best_domain = "knowledge_intake"
    best_score = 0
    for domain, route in _load_domains().items():
        values = [domain, route.get("title", "")]
        values.extend(_string_list(route.get("aliases")))
        values.extend(_string_list(route.get("keywords")))
        score = 0
        for value in values:
            token = str(value).casefold().strip()
            if _matches_route_token(token, haystack, haystack_terms):
                score += 3 if token == domain else 1
        if score > best_score:
            best_score = score
            best_domain = domain
    return best_domain


def _source_type(path: Path) -> str:
    suffix = path.suffix.casefold()
    if suffix in {".pdf"}:
        return "pdf"
    if suffix in {".doc", ".docx"}:
        return "office_document"
    if suffix in {".xls", ".xlsx", ".ods"}:
        return "spreadsheet"
    if suffix in {".json", ".jsonl"}:
        return "structured_metadata"
    if suffix in {".md", ".txt"}:
        return "text_playbook_or_note"
    if suffix in {".py", ".js", ".html", ".css"}:
        return "code_or_static_asset"
    return "unknown_file"


def _target_updates(
    domain: str,
    source_type: str,
    path_text: str,
    *,
    safety_flags: list[str],
) -> list[dict[str, Any]]:
    unsafe_flags = [flag for flag in safety_flags if flag in UNSAFE_METADATA_FLAGS]
    if unsafe_flags:
        reason = "source path requires review before durable knowledge metadata can reference it"
        return [
            {
                "target": "docs/agent/knowledge_map.json",
                "domain": domain,
                "operation": "blocked_pending_safety_review",
                "review_required": True,
                "blocked": True,
                "reason": reason,
                "safety_flags": unsafe_flags,
            },
            {
                "target": "docs/agent/knowledge_annotations.jsonl",
                "domain": domain,
                "operation": "blocked_pending_safety_review",
                "review_required": True,
                "blocked": True,
                "reason": reason,
                "source_type": source_type,
                "safety_flags": unsafe_flags,
            },
        ]
    return [
        {
            "target": "docs/agent/knowledge_map.json",
            "domain": domain,
            "operation": "add_or_update_source_metadata",
            "review_required": True,
            "source_path": path_text,
        },
        {
            "target": "docs/agent/knowledge_annotations.jsonl",
            "domain": domain,
            "operation": "add_compact_annotation",
            "review_required": True,
            "source_type": source_type,
        },
    ]


def _durable_rule_suggestions(domain: str, source_type: str, safety_flags: list[str]) -> list[dict[str, Any]]:
    rules = [
        {
            "domain": domain,
            "proposal": "Store durable routing metadata and short rules only after review.",
            "reason": f"Source type {source_type} may be useful for future knowledge routing.",
        }
    ]
    if safety_flags:
        rules.append(
            {
                "domain": domain,
                "proposal": "Keep raw private content out of Git and manager memory.",
                "reason": "Safety flags were detected during intake classification.",
            }
        )
    return rules


def _safety_flags(path: Path, *, root: Path, exists: bool) -> list[str]:
    flags: list[str] = []
    lowered_parts = {part.casefold() for part in path.parts}
    lowered_name = path.name.casefold()
    lowered_path = str(path).casefold()
    if not exists:
        flags.append("missing_file")
    if _safe_relative(path, root) is None:
        flags.append("outside_project")
    if lowered_parts.intersection(PRIVATE_PATH_MARKERS):
        flags.append("private_path")
    if any(marker in lowered_path for marker in RAW_DATA_MARKERS):
        flags.append("raw_crm_email_or_finance_risk")
    if any(marker in lowered_name for marker in ["secret", "token", "password", ".env"]):
        flags.append("secret_risk")
    return list(dict.fromkeys(flags))


def _sample_text(path: Path, *, safety_flags: list[str]) -> str:
    if any(flag in safety_flags for flag in UNSAFE_METADATA_FLAGS):
        return ""
    if not path.exists() or path.suffix.casefold() not in TEXT_EXTENSIONS:
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="ignore")[:20000]
    except OSError:
        return ""


def _apply_result(*, apply: bool, apply_allowed: bool) -> dict[str, Any]:
    if not apply:
        return {"status": "dry_run_only", "applied_changes": []}
    if not apply_allowed:
        return {
            "status": "blocked",
            "applied_changes": [],
            "reason": "source requires review or is not safe for automatic metadata apply",
        }
    return {
        "status": "deferred_for_review",
        "applied_changes": [],
        "reason": "v1 does not mutate knowledge metadata without explicit reviewed patch",
    }


def _load_domains() -> dict[str, Any]:
    if not KNOWLEDGE_MAP_PATH.exists():
        return {}
    try:
        payload = json.loads(KNOWLEDGE_MAP_PATH.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    domains = payload.get("domains") or {}
    if not isinstance(domains, dict):
        return {}
    normalized: dict[str, Any] = {}
    for domain, route in domains.items():
        if not isinstance(route, dict):
            continue
        normalized_route = dict(route)
        for field in _DOMAIN_LIST_FIELDS:
            normalized_route[field] = _string_list(normalized_route.get(field))
        normalized[str(domain)] = normalized_route
    return normalized


def _safe_relative(path: Path, root: Path) -> str | None:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return None


def _safe_path_text(path: Path, *, root: Path, safety_flags: list[str]) -> str:
    relative = _safe_relative(path, root)
    if relative is not None:
        return relative
    suffix = path.suffix.casefold()
    return f"<outside_project>/{_safe_file_label(path.name, suffix=suffix, safety_flags=safety_flags)}"


def _safe_file_label(name: str, *, suffix: str, safety_flags: list[str]) -> str:
    lowered = name.casefold()
    if "secret_risk" in safety_flags or any(marker in lowered for marker in ["secret", "token", "password", ".env"]):
        return f"redacted{suffix}"
    return name or f"unnamed{suffix}"


def _terms(text: str) -> list[str]:
    return TERM_PATTERN.findall(text.casefold())


def _matches_route_token(token: str, haystack: str, haystack_terms: list[str]) -> bool:
    if not token:
        return False
    token_terms = _terms(token)
    if not token_terms:
        return token in haystack
    if len(token_terms) == 1:
        return token_terms[0] in haystack_terms
    last_start = len(haystack_terms) - len(token_terms)
    return any(haystack_terms[index : index + len(token_terms)] == token_terms for index in range(last_start + 1))
