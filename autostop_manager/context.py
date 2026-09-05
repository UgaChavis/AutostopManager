from __future__ import annotations

from typing import Any

from .knowledge_base import plan_command_routes, probe_knowledge_base
from .storage import ManagerMemoryStore, load_manager_rules


SOURCE_BOUNDARIES = {
    "crm": "live service records",
    "store": "live catalog, stock, quotes and orders",
    "manager_memory": "routing and de-identified continuity",
    "gmail": "live mail",
    "telegram": "live messages and media",
    "store_analytics": "aggregate Store metrics",
}

EFFECT_CHECKS = {
    "crm_write": "Confirm the intended CRM record reflects the change.",
    "store_write": "Confirm the intended Store record reflects the change.",
    "document": "Inspect the generated document before use.",
    "external_send": "Confirm the intended recipient received one result.",
    "account_auth": "Confirm the selected account is authorized.",
    "remote_diagnostics": "Confirm actions against current device state.",
    "finance": "Reconcile the amounts and resulting business state.",
    "destructive": "Confirm the intended scope changed and recovery remains possible.",
}


def prepare_manager_context(
    store: ManagerMemoryStore | None,
    query: str,
    *,
    intent: str | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    del store
    query = (query or "").strip()
    limit = max(1, min(limit, 50))

    command_routes = plan_command_routes(query, intent=intent)
    first_route = command_routes[0] if command_routes else None
    preferred_domains = list(
        dict.fromkeys(domain for route in command_routes for domain in route.get("knowledge_domains", []) if domain)
    )
    knowledge = probe_knowledge_base(
        None,
        query,
        limit=max(limit, min(len(preferred_domains) + 2, 20)),
        preferred_domains=preferred_domains,
    )
    return {
        "ok": True,
        "query": query,
        "intent": intent or (first_route or {}).get("intent"),
        "command_routes": command_routes,
        "knowledge": {
            "has_knowledge": knowledge.get("has_knowledge"),
            "best_domain": knowledge.get("best_domain"),
            "open_first": knowledge.get("open_first"),
            "confidence": knowledge.get("confidence"),
            "source_of_truth": knowledge.get("source_of_truth", []),
            "reference_files": knowledge.get("reference_files", []),
            "optional_runtime_files": knowledge.get("optional_runtime_files", []),
            "optional_available_files": knowledge.get("optional_available_files", []),
            "optional_missing_files": knowledge.get("optional_missing_files", []),
            "optional_runtime_available": knowledge.get("optional_runtime_available", False),
            "optional_runtime_note": knowledge.get("optional_runtime_note", ""),
            "routes": knowledge.get("routes", []),
        },
    }


def _route_navigation(knowledge: dict[str, Any], domain: str | None) -> dict[str, Any]:
    return next(
        (route for route in knowledge.get("routes", []) if route.get("domain") == domain),
        {},
    )


def _route_step(route: dict[str, Any], knowledge: dict[str, Any]) -> dict[str, Any]:
    domain = str(route.get("domain") or "") or None
    navigation = _route_navigation(knowledge, domain)
    return {
        "command_id": route.get("command_id"),
        "workflow_id": route.get("workflow_id") or route.get("command_id"),
        "intent": route.get("intent"),
        "priority": int(route.get("priority") or 0),
        "phase": int(route.get("phase") or 0),
        "dependencies": list(route.get("dependencies") or []),
        "effects": list(route.get("effects") or []),
        "knowledge_domains": list(route.get("knowledge_domains") or []),
        "domain": domain,
        "open_first": navigation.get("open_first"),
        "source_of_truth": navigation.get("source_of_truth", []),
        "confidence": route.get("confidence"),
        "uncertainty": route.get("uncertainty"),
        "matching_terms": list(route.get("matching_terms") or []),
    }


def build_agent_brief(
    store: ManagerMemoryStore | None,
    query: str,
    *,
    intent: str | None = None,
    limit: int = 8,
) -> dict[str, Any]:
    context = prepare_manager_context(store, query, intent=intent, limit=limit)
    knowledge = context.get("knowledge", {})
    command_routes = list(context.get("command_routes") or [])
    steps = [_route_step(route, knowledge) for route in command_routes]
    first = steps[0] if steps else {}
    domain = str(first.get("domain") or "")
    effects = list(dict.fromkeys(effect for step in steps for effect in step.get("effects", [])))
    verification = ["Confirm the result solves the request."]
    verification.extend(EFFECT_CHECKS[effect] for effect in effects if effect in EFFECT_CHECKS)
    candidates = [
        {
            key: candidate.get(key)
            for key in ("workflow_id", "domain", "open_first", "confidence", "uncertainty", "matching_terms")
        }
        for candidate in sorted(
            knowledge.get("routes", []) if not steps else [],
            key=lambda item: int(item.get("score") or 0),
            reverse=True,
        )[:3]
        if candidate.get("score", 0) > 0
    ]
    if not steps:
        for candidate in candidates:
            candidate["confidence"] = min(float(candidate.get("confidence") or 0), 0.49)
            candidate["uncertainty"] = round(1.0 - float(candidate["confidence"]), 2)
    source_of_truth = list(dict.fromkeys(source for step in steps for source in step.get("source_of_truth", [])))
    route_domains = {str(value) for step in steps for value in step.get("knowledge_domains", [])}
    write_domains = []
    if "crm_write" in effects:
        write_domains.append("crm")
    if "store_write" in effects:
        write_domains.append("store")
    external_connectors = [
        connector
        for connector, marker in (
            ("gmail", "gmail"),
            ("telegram", "telegram"),
            ("store", "store"),
            ("home_camera", "home_camera"),
            ("public_camera", "public_camera"),
            ("remote_diagnostics", "remote_diagnostics"),
        )
        if any(marker in route_domain for route_domain in route_domains)
    ]
    canonical_rules = [str(item["rule"]) for item in load_manager_rules()]

    return {
        "ok": True,
        "format": "agent_brief_v1",
        "query": context.get("query"),
        "intent": context.get("intent"),
        "role": "AutoStop operations director agent",
        "language": "ru",
        "answer_style": "natural, adaptive, outcome-driven",
        "route": {
            "steps": steps,
            "source_of_truth": source_of_truth,
            "reference_files": _route_navigation(knowledge, domain).get("reference_files", []) if steps else [],
            "optional_runtime_files": _route_navigation(knowledge, domain).get("optional_runtime_files", [])
            if steps
            else [],
            "optional_available_files": _route_navigation(knowledge, domain).get("optional_available_files", [])
            if steps
            else [],
            "optional_missing_files": _route_navigation(knowledge, domain).get("optional_missing_files", [])
            if steps
            else [],
            "optional_runtime_available": _route_navigation(knowledge, domain).get("optional_runtime_available", False)
            if steps
            else False,
            "optional_runtime_note": _route_navigation(knowledge, domain).get("optional_runtime_note", "")
            if steps
            else "",
            "selection_mode": "recommended" if steps else "explore",
            "candidates": candidates,
            "write_domains": write_domains,
            "external_connectors": external_connectors,
        },
        "source_boundaries": SOURCE_BOUNDARIES,
        "hot_rules": canonical_rules,
        "verification": verification,
    }
