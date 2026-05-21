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


GENERAL_HOT_RULES = [
    "AutoStop CRM is the source of truth for cards, clients, vehicles, repair orders, payments, cashboxes, files, and live board state.",
    "AutostopManager memory stores only durable non-CRM context: owner preferences, rules, lessons, tasks, reminders, and short conclusions.",
    "Use Obsidian as a human-readable knowledge layer only; do not store raw client databases, cash journals, full repair orders, full board dumps, secrets, or raw email threads there.",
    "Before CRM writes, identify the exact target id, write patch-only confirmed fields, then reread the target and verify the result.",
]

DOMAIN_BRIEF_RULES = {
    "board_cleanup_autopilot": [
        "Routine board cleanup may update confirmed fields, tags, deadlines, indicators, source-backed vehicle profile fields, concise AI notes, and board_summary.",
        "Routine board cleanup must not move cards between columns and must not archive cards unless the owner gives a separate explicit command.",
        "Preserve operator evidence: works, materials, prices, payments, files, contacts, VIN/chassis/license data, diagnostics, and historical notes.",
        "Use description as a short readable working note: paragraphs when useful, important facts plus next action, restrained emoji/rich-text accents that render cleanly, and no raw HTML/pseudo-formatting/visible technical markup; keep board_summary a plain 4-5 line preview.",
        "After saving CRM description, inspect the visible text/preview and remove formatting artifacts immediately.",
        "Keep source lists, long explanations, phone, VIN, full client name, raw diagnostic dumps, rich formatting, emoji decoration, and long issue lists out of board_summary.",
    ],
}

DEFAULT_READ_ORDER = [
    "prepare_manager_context",
    "probe_knowledge_base",
    "open returned open_first/source_of_truth",
    "use focused reads before broad exports",
]

BOARD_CLEANUP_READ_ORDER = [
    "today_context",
    "bootstrap_context",
    "get_board_context or review_board",
    "search_cards/get_card_context for focused targets",
    "list_repair_orders/get_repair_order when money, works, materials, ready state, or closure matters",
    "get_cashbox/get_cash_journal only when payment evidence must be checked",
]

BOARD_CLEANUP_ALLOWED_ACTIONS = [
    "read live CRM board/card/order/cashbox context",
    "update confirmed title, vehicle, description, tags, deadline, indicator, and source-backed vehicle profile fields",
    "set_card_board_summary",
    "add one concise AI note or question when it changes the next action",
    "record manager run events and a short manager_journal after meaningful work",
]

BOARD_CLEANUP_FORBIDDEN_ACTIONS = [
    "move_card or bulk_move_cards without a separate explicit owner command naming the target and destination",
    "archive_card without a separate explicit owner command for archive",
    "delete or overwrite operator-entered works, materials, prices, payments, files, contacts, diagnostics, or historical notes",
    "change payments, cashboxes, repair-order works/materials, files, clients, or repair orders without explicit owner intent for that exact target",
    "put phone, VIN, full client name, long complaint text, or raw diagnostic dumps into board_summary",
]

BOARD_CLEANUP_VERIFICATION = [
    "reread every written card with get_card_context or get_card",
    "verify board_summary_stale=false after summary/content/profile/tag changes",
    "report cards_moved=0 and cards_archived=0 unless the owner explicitly commanded those actions",
    "record unresolved blockers and skipped writes instead of guessing",
]

DEFAULT_ALLOWED_ACTIONS = [
    "read manager memory and local knowledge routes",
    "open the returned source-of-truth files",
    "use focused CRM/Gmail reads when the task requires live state",
]

DEFAULT_FORBIDDEN_ACTIONS = [
    "write to CRM, Gmail, Obsidian, or files without task-specific owner intent",
    "copy raw CRM records, cashbox ledgers, full repair orders, raw email threads, or secrets into memory or docs",
]

DEFAULT_VERIFICATION = [
    "state which source-of-truth files or live tools were used",
    "state missing required context instead of inventing facts",
]

MEMORY_SOURCES = {
    "local_sqlite": "knowledge_index_and_local_rules",
    "crm_mcp": "operational_memory_and_live_board_context",
    "rule": "before CRM work, read live MCP context; before broad docs, use local knowledge routes",
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


def _compact_hot_rules(domain: str | None, limit: int) -> list[str]:
    rules = [*GENERAL_HOT_RULES, *DOMAIN_BRIEF_RULES.get(str(domain or ""), [])]
    return rules[: max(1, min(limit, 8))]


def build_agent_brief(
    store: ManagerMemoryStore | None,
    query: str,
    *,
    intent: str | None = None,
    limit: int = 8,
) -> dict[str, Any]:
    context = prepare_manager_context(store, query, intent=intent, limit=limit)
    knowledge = context.get("knowledge", {})
    command_route = context.get("command_route") or {}
    domain = str(knowledge.get("best_domain") or command_route.get("domain") or "")

    if domain == "board_cleanup_autopilot":
        read_order = BOARD_CLEANUP_READ_ORDER
        allowed_actions = BOARD_CLEANUP_ALLOWED_ACTIONS
        forbidden_actions = BOARD_CLEANUP_FORBIDDEN_ACTIONS
        verification = BOARD_CLEANUP_VERIFICATION
        next_actions = [
            "read live CRM context",
            "classify blockers",
            "write only meaningful confirmed deltas",
            "refresh and verify board_summary",
            "report counts, skipped writes, blockers, and risks",
        ]
    else:
        read_order = DEFAULT_READ_ORDER
        allowed_actions = DEFAULT_ALLOWED_ACTIONS
        forbidden_actions = DEFAULT_FORBIDDEN_ACTIONS
        verification = DEFAULT_VERIFICATION
        next_actions = list(context.get("next_actions") or [])

    return {
        "ok": True,
        "format": "agent_brief_v1",
        "query": context.get("query"),
        "intent": context.get("intent"),
        "role": "AutoStop CRM manager agent",
        "language": "ru",
        "answer_style": "short, practical, direct",
        "memory_sources": MEMORY_SOURCES,
        "route": {
            "command_id": command_route.get("command_id"),
            "domain": domain or None,
            "open_first": knowledge.get("open_first"),
            "source_of_truth": knowledge.get("source_of_truth", []),
            "confidence": knowledge.get("confidence"),
        },
        "source_boundaries": {
            "crm": "live source of truth for cards, clients, vehicles, repair orders, payments, cashboxes, files, and board state",
            "manager_memory": "durable non-CRM context, rules, lessons, tasks, reminders, and short conclusions",
            "gmail": "source of truth for raw email messages, threads, drafts, labels, attachments, and sent history",
            "obsidian": "human-readable knowledge layer and safe summaries only",
        },
        "hot_rules": _compact_hot_rules(domain, limit),
        "read_order": read_order,
        "allowed_actions": allowed_actions,
        "forbidden_actions": forbidden_actions,
        "required_context": context.get("required_context", []),
        "missing_context": context.get("missing_context", []),
        "next_actions": next_actions,
        "verification": verification,
    }
