from __future__ import annotations

from typing import Any

from .knowledge_base import find_command_route, probe_knowledge_base
from .storage import ManagerMemoryStore


DOMAIN_REQUIRED_CONTEXT_DEFAULTS = {
    "bmw_f15_n63": ["VIN or chassis", "production date", "market", "BMW fault memory with module names"],
    "service_management": ["live CRM board state"],
    "crm_vin_oem_parts_lookup": ["live CRM card id", "VIN or frame/body number", "requested part", "repair-order target if materials will be written"],
    "vehicle_identity_and_oem": ["VIN or chassis"],
    "fluids": ["VIN or chassis", "market", "engine code", "transmission code", "exact unit"],
}


GENERAL_HOT_RULES = [
    "AutoStop CRM is the source of truth for cards, clients, vehicles, repair orders, payments, cashboxes, files, and live board state.",
    "AutostopManager memory stores only durable non-CRM context: owner preferences, rules, lessons, tasks, reminders, and short conclusions.",
    "Do not store raw client databases, cash journals, full repair orders, full board dumps, secrets, or raw email threads in manager memory or docs.",
    "Before CRM writes, identify the exact target id, write patch-only confirmed fields, then reread the target and verify the result.",
]

DOMAIN_BRIEF_RULES = {
    "board_cleanup_autopilot": [
        "Routine board cleanup focuses on the card itself: confirmed title/vehicle/description, rare operational tags capped at three, source-backed vehicle profile fields, client link/client record enrichment, concise AI notes, and board_summary.",
        "Routine board cleanup must not move cards between columns and must not archive cards unless the owner gives a separate explicit command.",
        "Preserve operator evidence while moving structured data to structured fields: phone to client, VIN/plate/mileage/engine/gearbox/drivetrain to vehicle_profile; after verified transfer, do not keep those raw identifiers in the public description by default.",
        "For 'Приберись', first inspect vehicle passport and client data; phone is the primary client match key, source-backed vehicle fields include engine/gearbox/drivetrain when evidence is adequate, and description is a very short formatted summary: if empty leave it empty, otherwise preserve prices/OEM/facts with **bold**, *italic*, ++underline++, and sparse emoji.",
        "Do not put 'Статус:', 'Следующий шаг:', source lists, safety disclaimers, or 'нужно перепроверить данные' caveats into the public description.",
        "Keep vehicle as compact make/model and title as the short issue/work essence; keep board_summary a plain 4-5 line factual preview without private data or decorative formatting.",
        "If the card description contains a direct safe task such as find parts, find OEM, price maintenance, or decode VIN, do it during cleanup and write back only a compact result.",
        "Update a live repair order, works, materials, prices, payments, cashboxes, or cash records only when the owner explicitly asks for that exact target.",
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
    "manager_board_scan",
    "triage_inbox_cards/list_ready_unpaid_cards/list_cards_missing_manager_data when relevant",
    "audit_client_links/suggest_clients_for_card/search_clients/get_client when client data is incomplete or ambiguous",
    "search_cards/get_card_context for focused targets",
    "list_repair_orders/get_repair_order when money, works, materials, ready state, or closure matters",
    "get_cashbox/get_cash_journal only when payment evidence must be checked",
]

BOARD_CLEANUP_ALLOWED_ACTIONS = [
    "read live CRM board/card/client/order/cashbox context",
    "use high-level CRM manager operations in dry_run before apply",
    "cleanup_card for compact safe one-card patches",
    "update confirmed title, vehicle, short description, at most three rare operational tags, and source-backed vehicle profile fields including engine/gearbox/drivetrain when evidence is adequate",
    "move phone/VIN/plate/mileage/aggregate facts into structured fields, link/update the clear matching client by phone first, and upsert confirmed client vehicle facts",
    "set_card_board_summary",
    "add one concise AI note or question only when it adds a factual blocker, missing data, or verified conclusion",
    "execute direct safe card tasks such as VIN decode, OEM/parts lookup, or maintenance price estimate and write back only the compact result",
    "update repair_order only when the owner explicitly asks to fill or расписывать the target ЗН/заказ-наряд",
    "record manager run events and a short manager_journal after meaningful work",
]

BOARD_CLEANUP_FORBIDDEN_ACTIONS = [
    "move_card or bulk_move_cards without a separate explicit owner command naming the target and destination",
    "archive_card without a separate explicit owner command for archive",
    "delete or overwrite operator-entered works, materials, prices, payments, files, contacts, diagnostics, or historical notes",
    "change payments, cashboxes, repair-order works/materials/prices/totals/status, files, or repair orders without explicit owner intent for that exact target",
    "delete or merge client records during routine cleanup",
    "change card deadlines or indicators during routine cleanup unless the owner explicitly asks for timer/signal work for the exact target",
    "invent text for an empty public description during routine cleanup",
    "leave phone, VIN, license plate, mileage, engine, gearbox, or drivetrain in public description after verified structured transfer unless operationally needed",
    "put phone, VIN, full client name, long complaint text, or raw diagnostic dumps into board_summary",
]

BOARD_CLEANUP_VERIFICATION = [
    "reread every written card with get_card_context or get_card",
    "verify board_summary_stale=false after summary/content/profile/tag changes",
    "verify client link/client vehicle changes after writes when cleanup touched client data",
    "report cards_moved=0 and cards_archived=0 unless the owner explicitly commanded those actions",
    "report repair_orders_changed=0 and payments_changed=0 unless the owner explicitly commanded those actions",
    "record unresolved blockers and skipped writes instead of guessing",
]

LONG_RUN_CONTEXT_SAFETY = {
    "why": "Board-wide CRM tasks can outgrow the chat context; durable progress must live outside the model window.",
    "rules": [
        "Start start_manager_run before broad CRM scans, multi-card cleanup, procurement sweeps, finance checks, or knowledge-intake batches.",
        "Record checkpoint events after scope selection, candidate filtering, each write batch, each skip batch, and each verification batch.",
        "Keep raw board snapshots, full card dumps, phone lists, VIN/license tables, and repair-order dumps out of chat; save full machine data to local private files only when needed and report compact counts.",
        "Prefer compact manager tools and focused get_card_context/get_repair_order reads over full-board Markdown or full JSON output.",
        "Process large CRM work in small verified batches and leave a resume point in the run ledger before continuing.",
    ],
    "checkpoint_event_types": [
        "planned_action",
        "checkpoint",
        "skip",
        "write",
        "risk",
        "verification",
    ],
    "recovery": [
        "After a stalled or compacted thread, call list_manager_runs(include_events=true) and resume from the latest running run.",
        "If the Codex UI fails immediately after automatic context compaction with an invalid enum for context_compaction, restart the Codex app-server/Desktop so the active process matches the installed CLI.",
    ],
}

DEFAULT_ALLOWED_ACTIONS = [
    "read manager memory and local knowledge routes",
    "open the returned source-of-truth files",
    "use focused CRM/Gmail reads when the task requires live state",
]

DEFAULT_FORBIDDEN_ACTIONS = [
    "write to CRM, Gmail, or files without task-specific owner intent",
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
    if "live crm card id" in name:
        return bool(
            "card_id" in lowered
            or "card id" in lowered
            or "карточка #" in lowered
            or "карточка №" in lowered
            or "card #" in lowered
        )
    if "vin" in name or "chassis" in name:
        return "vin" in lowered or "кузов" in lowered or "frame" in lowered or "chassis" in lowered
    if "requested part" in name:
        return any(
            term in lowered
            for term in [
                "детал",
                "запчаст",
                "свеч",
                "колод",
                "фильтр",
                "рейк",
                "сцеплен",
                "датчик",
                "ремень",
                "насос",
                "part",
                "filter",
                "plug",
                "pads",
                "rack",
                "clutch",
                "sensor",
            ]
        )
    if "repair-order" in name or "materials" in name:
        return any(term in lowered for term in ["repair order", "заказ-наряд", "зн", "материал"])
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
    if intent:
        recall_queries.append(intent)

    memory_context = memory.memory_context_for(query, limit=limit)
    relevant: list[dict[str, Any]] = [
        *memory_context.get("preferences_or_facts", []),
        *memory_context.get("lessons", []),
    ]
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
            "reference_files": knowledge.get("reference_files", []),
            "optional_runtime_files": knowledge.get("optional_runtime_files", []),
            "optional_available_files": knowledge.get("optional_available_files", []),
            "optional_missing_files": knowledge.get("optional_missing_files", []),
            "optional_runtime_available": knowledge.get("optional_runtime_available", False),
            "optional_runtime_note": knowledge.get("optional_runtime_note", ""),
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
            "reference_files": knowledge.get("reference_files", []),
            "optional_runtime_files": knowledge.get("optional_runtime_files", []),
            "optional_available_files": knowledge.get("optional_available_files", []),
            "optional_missing_files": knowledge.get("optional_missing_files", []),
            "optional_runtime_available": knowledge.get("optional_runtime_available", False),
            "optional_runtime_note": knowledge.get("optional_runtime_note", ""),
            "confidence": knowledge.get("confidence"),
        },
        "source_boundaries": {
            "crm": "live source of truth for cards, clients, vehicles, repair orders, payments, cashboxes, files, and board state",
            "manager_memory": "durable non-CRM context, rules, lessons, tasks, reminders, and short conclusions",
            "gmail": "source of truth for raw email messages, threads, drafts, labels, attachments, and sent history",
        },
        "hot_rules": _compact_hot_rules(domain, limit),
        "read_order": read_order,
        "allowed_actions": allowed_actions,
        "forbidden_actions": forbidden_actions,
        "required_context": context.get("required_context", []),
        "missing_context": context.get("missing_context", []),
        "context_safety": LONG_RUN_CONTEXT_SAFETY,
        "next_actions": next_actions,
        "verification": verification,
    }
