from __future__ import annotations

from typing import Any

from .knowledge_base import find_command_route, probe_knowledge_base
from .storage import ManagerMemoryStore


DOMAIN_REQUIRED_CONTEXT_DEFAULTS = {
    "bmw_f15_n63": ["VIN or chassis", "production date", "market", "BMW fault memory with module names"],
    "service_management": ["live CRM board state"],
    "vehicle_identity_and_oem": ["VIN or chassis"],
    "fluids": ["VIN or chassis", "market", "engine code", "transmission code", "exact unit"],
}


def _unique_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    best_by_key: dict[tuple[str, int], dict[str, Any]] = {}
    for item in items:
        key = (str(item.get("kind") or ""), int(item.get("id") or 0))
        existing = best_by_key.get(key)
        if existing is None or float(item.get("score") or 0) > float(existing.get("score") or 0):
            best_by_key[key] = item
    result = list(best_by_key.values())
    result.sort(
        key=lambda item: (
            float(item.get("score") or 0),
            item.get("updated_at") or item.get("created_at") or "",
        ),
        reverse=True,
    )
    return result


def _query_has_context(query: str, context_name: str) -> bool:
    lowered = query.casefold()
    name = context_name.casefold()
    if "vin" in name or "chassis" in name:
        return "vin" in lowered or "кузов" in lowered or "frame" in lowered or "chassis" in lowered
    if "market" in name:
        return "market" in lowered or "рынок" in lowered
    if "engine" in name:
        return "engine" in lowered or "мотор" in lowered or "двигатель" in lowered
    if "transmission" in name:
        return "transmission" in lowered or "короб" in lowered or "кпп" in lowered
    return False


def prepare_manager_context(
    store: ManagerMemoryStore | None,
    query: str,
    *,
    intent: str | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    memory = store or ManagerMemoryStore()
    memory.initialize()
    query = (query or "").strip()
    limit = max(1, min(limit, 50))

    command_route = find_command_route(query, intent=intent)
    knowledge = probe_knowledge_base(memory, query, limit=limit)
    recall_queries: list[str] = []
    if command_route:
        recall_queries.extend(command_route.get("memory_queries", []))
        # Command routes are owner-intent level and should override broad domain open order.
        knowledge["best_domain"] = command_route.get("domain") or knowledge.get("best_domain")
        knowledge["open_first"] = command_route.get("open_first") or knowledge.get("open_first")
        knowledge["has_knowledge"] = True
        knowledge["command_route"] = command_route
    recall_queries.append(query)
    if intent:
        recall_queries.append(intent)

    relevant: list[dict[str, Any]] = []
    for recall_query in dict.fromkeys(item for item in recall_queries if item):
        relevant.extend(memory.recall(recall_query, limit=limit).get("items", []))
    relevant = _unique_items(relevant)[:limit]

    required_context = []
    for route in knowledge.get("routes", []):
        if route.get("domain") == knowledge.get("best_domain"):
            required_context = list(route.get("required_context") or [])
            break
    if not required_context:
        required_context = DOMAIN_REQUIRED_CONTEXT_DEFAULTS.get(str(knowledge.get("best_domain") or ""), [])
    missing_context = [item for item in required_context if not _query_has_context(query, item)]

    return {
        "ok": True,
        "query": query,
        "intent": intent or (command_route or {}).get("intent"),
        "command_route": command_route,
        "knowledge": {
            "has_knowledge": knowledge.get("has_knowledge"),
            "best_domain": knowledge.get("best_domain"),
            "open_first": knowledge.get("open_first"),
            "confidence": knowledge.get("confidence"),
            "source_of_truth": knowledge.get("source_of_truth", []),
            "routes": knowledge.get("routes", []),
        },
        "relevant_memory": relevant,
        "required_context": required_context,
        "missing_context": missing_context,
        "next_actions": list((command_route or {}).get("next_actions") or [knowledge.get("next_action")]),
    }
