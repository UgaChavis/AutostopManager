from __future__ import annotations

from typing import Any

from .knowledge_base import find_command_route, probe_knowledge_base
from .storage import ManagerMemoryStore


DOMAIN_REQUIRED_CONTEXT_DEFAULTS: dict[str, list[str]] = {
    "bmw_f15_n63": ["VIN or chassis", "production date", "market", "BMW fault memory with module names"],
    "service_management": ["live CRM board state"],
    "crm_vin_oem_parts_lookup": [
        "live CRM card id",
        "VIN or frame/body number",
        "requested part",
        "repair-order target if materials will be written",
    ],
    "vehicle_identity_and_oem": ["VIN or chassis"],
    "fluids": ["VIN or chassis", "market", "engine code", "transmission code", "exact unit"],
    "store_management": [],
}


GENERAL_HOT_RULES = [
    "AutoStop CRM is the source of truth for cards, clients, vehicles, repair orders, payments, cashboxes, files, and live board state.",
    "AutostopManager memory stores only durable non-CRM context: owner preferences, rules, lessons, tasks, reminders, and short conclusions.",
    "Do not store raw client databases, store orders or stock rows, cash journals, full repair orders, full board dumps, secrets, or raw email threads in manager memory or docs.",
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
    "store_management": [
        "AutoStop App is the source of truth for store facts; read them only through its pure-read agent API, never the store database or mutating legacy GET endpoints.",
        "Use the stateless one-request Store bootstrap snapshot for startup health and store_digest for owner 'what is new' reads so startup never consumes owner-visible changes.",
        "Interpret store business dates such as today in Asia/Krasnoyarsk; keep Manager checkpoint timestamps technical UTC and cursors opaque.",
        "General Store reads stay redacted. An exact store_quote_request detail=full read uses the dedicated quote credential and may expose contacts, VIN, request items, offers, notes, and drafts transiently; never persist that payload in Manager memory.",
        "Store writes are limited to quote assignment/status/internal comment/append-only note/private drafts, exact batch storage location, and exact IN_PROGRESS to READY order transition.",
        "Every store write requires exact reread, ActionContractV2, expected_updated_at, unique idempotency key, dry_run, apply, correlation id, exact readback, and compact refs-only ledger data.",
        "Never change store prices, quantities, products, customers, finance, ROSSKO orders, marketplace publication, or arbitrary settings through this route.",
    ],
    "knowledge_intake": [
        "For documentation hygiene, inventory tracked docs and their reference graph before editing or deleting anything.",
        "Prefer the smallest existing canonical file; migrate unique active rules before deleting an obsolete document.",
        "Run cleanup-audit before deletion, then knowledge-sync, knowledge-audit, annotations-audit, and skills-audit after durable documentation changes.",
    ],
    "remote_codex_access": [
        "Keep the managed-pc fleet and legacy home-pc route independent; never reuse or rotate one route's keys while operating on the other.",
        "For managed-pc, resolve the exact alias and run status before shell, run, PowerShell, copy, repair, rename, or revoke operations.",
        "Never print private keys, passwords, tokens, USB enrollment credentials, or protected runtime state.",
    ],
}

DEFAULT_READ_ORDER = [
    "prepare_manager_context",
    "probe_knowledge_base",
    "open returned open_first/source_of_truth",
    "use focused reads before broad exports",
]

BOARD_CLEANUP_READ_ORDER = [
    "agent_bootstrap",
    "agent_board_digest",
    "agent_board_workflow(operation=manager_board_scan, mode=dry_run)",
    "agent_board_workflow for triage_inbox_cards/list_ready_unpaid_cards/list_cards_missing_manager_data when relevant",
    "agent_board_workflow(operation=audit_client_links) and agent_search(entity=client) when client data is incomplete or ambiguous",
    "agent_search(entity=card) and agent_entity_context for focused targets",
    "agent_search(entity=repair_order) and agent_entity_context when money, works, materials, ready state, or closure matters",
    "agent_finance_workflow for cashbox evidence only when payment evidence must be checked",
]

BOARD_CLEANUP_ALLOWED_ACTIONS = [
    "read live CRM board/card/client/order/cashbox context",
    "use high-level CRM manager operations in dry_run before apply",
    "agent_board_workflow(operation=cleanup_card) for compact safe one-card patches, dry_run before apply",
    "update confirmed title, vehicle, short description, at most three rare operational tags, and source-backed vehicle profile fields including engine/gearbox/drivetrain when evidence is adequate",
    "move phone/VIN/plate/mileage/aggregate facts into structured fields, link/update the clear matching client by phone first, and upsert confirmed client vehicle facts",
    "update board_summary only through the named card-cleanup workflow",
    "add one concise AI note or question only when it adds a factual blocker, missing data, or verified conclusion",
    "execute direct safe card tasks such as VIN decode, OEM/parts lookup, or maintenance price estimate and write back only the compact result",
    "update repair_order only when the owner explicitly asks to fill or расписывать the target ЗН/заказ-наряд",
    "checkpoint the Gateway v2 workflow and add a short manager_journal entry through raw discovery only when the conclusion is durable",
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
    "reread every written card with agent_entity_context",
    "verify board_summary_stale=false after summary/content/profile/tag changes",
    "verify client link/client vehicle changes after writes when cleanup touched client data",
    "report cards_moved=0 and cards_archived=0 unless the owner explicitly commanded those actions",
    "report repair_orders_changed=0 and payments_changed=0 unless the owner explicitly commanded those actions",
    "record unresolved blockers and skipped writes instead of guessing",
]

STORE_ANALYTICS_READ_ORDER = [
    "open docs/agent/store_analytics_playbook.md",
    "discover get_store_analytics_report through Gateway v2 raw discovery",
    "call get_store_analytics_report with the original natural query and requested period",
    "answer from aggregate summary, rankings, funnel, and previous-period comparison only",
]

STORE_ANALYTICS_ALLOWED_ACTIONS = [
    "read the protected aggregate-only storefront report",
    "compare the selected period with the previous equal-duration period in Asia/Krasnoyarsk",
    "report visitors, sessions, page views, engaged time, top pages/products, search quality, interactions, and funnel rates",
]

STORE_ANALYTICS_FORBIDDEN_ACTIONS = [
    "request, expose, or persist raw analytics events or visitor/session identifiers",
    "request or infer IP, User-Agent, exact search text, form contents, customer identity, contacts, VIN, or click coordinates",
    "write analytics results to CRM or durable Manager memory",
    "claim legal compliance from the technical implementation alone",
]

STORE_ANALYTICS_VERIFICATION = [
    "verify store_analytics_report_v1 and Asia/Krasnoyarsk",
    "verify meta.aggregatedOnly=true and rawEventsIncluded=false",
    "verify the output contains no raw/private identifier keys",
    "state the selected period and previous-period comparison",
]

STORE_READ_ORDER = [
    "agent_bootstrap for compact CRM plus a one-request stateless Store readiness snapshot",
    "agent_board_digest(scope=store) for store digest and owner-visible store_digest cursor",
    "agent_search with an exact store entity for bounded lists and catalog/stock lookup",
    "agent_entity_context with an exact store entity/id; general reads stay redacted, while store_quote_request detail=full uses the dedicated quote credential for transient contacts, VIN, items, offers, notes, and drafts",
    "agent_inventory_workflow only for an allowlisted store write after prepare_action_contract",
]

STORE_ALLOWED_ACTIONS = [
    "read compact store digest, order and quote-request lists, catalog parts, stock totals, batches and storage locations, warehouse operations, suppliers, marketplace errors, and one exact full quote transiently",
    "search store_sourcing_offer for bounded local and ROSSKO candidates without persisting raw quote or supplier payloads",
    "paginate every store list and resume an unfinished digest with its opaque next_cursor",
    "assign an exact quote request, toggle NEW and IN_PROGRESS, update its internal comment, append a note, replace private structured drafts, change an exact batch storage location, or mark an exact IN_PROGRESS order READY",
    "use dry_run then apply with expected_updated_at, idempotency key, correlation id, and exact reread for every allowlisted write",
]

STORE_FORBIDDEN_ACTIONS = [
    "read the AutoStop App database directly or call legacy store GET routes with side effects",
    "persist raw store orders, customer contacts, order lines, stock rows, warehouse dumps, VIN lists, or API payloads in Manager memory, docs, Git, or workflow ledger",
    "change prices, products, quantities, customers, finance, COMPLETE/ANNULLED/RETURNED state, ROSSKO orders, marketplace publication, or arbitrary settings",
    "perform any store write outside agent_inventory_workflow and the explicit seven-operation allowlist",
]

STORE_VERIFICATION = [
    "store outage degrades only store status and does not break CRM",
    "digest checkpoint advances only after the final page; failed or abandoned paging preserves the committed cursor",
    "exact store writes match planned fields after reread and include the audit correlation id",
    "Manager workflow state contains compact technical refs only and no raw store payload",
]

KNOWLEDGE_HYGIENE_READ_ORDER = [
    "open docs/agent/knowledge_shelves.md and the knowledge-probe results",
    "inventory tracked docs, route references, recent feature changes, and current audit baselines",
    "update the smallest canonical docs plus knowledge_map, annotations, rules, and command routes when routing changed",
    "run cleanup-audit before removing confirmed obsolete or generated artifacts",
]

KNOWLEDGE_HYGIENE_ALLOWED_ACTIONS = [
    "update canonical Manager documentation, routes, annotations, and rules",
    "remove generated caches and fully migrated obsolete documentation after reference checks",
    "run knowledge-sync, documentation audits, tests, lint, and health checks",
    "commit verified repository changes when the owner explicitly requests it",
]

KNOWLEDGE_HYGIENE_FORBIDDEN_ACTIONS = [
    "delete an active source-of-truth file or unique instruction before migrating its rules",
    "commit raw CRM or Store exports, runtime databases, secrets, tokens, private keys, or temporary remote-control scripts",
    "change CRM, Store, Gmail, or remote-PC business state as part of documentation cleanup",
]

KNOWLEDGE_HYGIENE_VERIFICATION = [
    "knowledge-probe routes documentation maintenance to knowledge_intake",
    "knowledge-sync, knowledge-audit, annotations-audit, skills-audit, and cleanup-audit pass",
    "focused and full available Manager quality gates pass",
    "git status contains only intentional verified changes before commit",
]

REMOTE_ACCESS_READ_ORDER = [
    "open docs/agent/codex_home_pc_reverse_ssh.md",
    "choose managed-pc or legacy home-pc without mixing their credentials or commands",
    "for managed-pc run doctor, resolve the exact alias, then run status before an operation",
    "for legacy home-pc run the documented loopback listener and BatchMode quick checks",
]

REMOTE_ACCESS_ALLOWED_ACTIONS = [
    "read compact server-side health and exact-device status",
    "use shell, run, PowerShell, scp/sftp, copy, repair, or rename on the exact owner-authorized machine when the task requires it",
    "refresh generated managed-pc SSH configs after a control-plane upgrade",
    "revoke or rotate credentials only when the owner explicitly requests that exact security action",
]

REMOTE_ACCESS_FORBIDDEN_ACTIONS = [
    "print, copy, commit, or expose private keys, passwords, tokens, USB enrollment credentials, or protected runtime state",
    "mix managed-pc and legacy home-pc credentials or rotate one route while operating on the other",
    "format disks, change bootloaders, mass-delete data, disable protection, reboot or shut down, or stop critical business services without a separate exact instruction",
]

REMOTE_ACCESS_VERIFICATION = [
    "report the exact alias or legacy route that was used",
    "reread status or run the documented health check after a change",
    "confirm secrets were neither printed nor committed",
]

LONG_RUN_CONTEXT_SAFETY = {
    "why": "Board-wide CRM tasks can outgrow the chat context; durable progress must live outside the model window.",
    "rules": [
        "For one named CRM operation, use its automatic Gateway ledger and do not call start_workflow separately. Start a parent workflow only for multi-operation CRM work, procurement sweeps, finance batches, CRM+Gmail work, or knowledge-intake batches.",
        "Use workflow_checkpoint after scope selection, candidate filtering, each write/skip/verification batch, and before any external connector wait.",
        "Keep raw board snapshots, full card dumps, phone lists, VIN/license tables, and repair-order dumps out of chat; save full machine data to local private files only when needed and report compact counts.",
        "Prefer agent_board_digest, agent_search, agent_entity_context, and named domain workflows over full-board Markdown, raw capabilities, or full JSON output.",
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
        "Call agent_bootstrap, then workflow_status and workflow_resume for the newest unfinished v2 workflow; external_wait resumes only after complete_external_step.",
        "After a stalled or compacted thread, use agent_bootstrap unfinished workflows, then workflow_status and workflow_resume with the latest state_version.",
        "If the Codex UI fails immediately after automatic context compaction with an invalid enum for context_compaction, restart the Codex app-server/Desktop so the active process matches the installed CLI.",
    ],
}

DEFAULT_ALLOWED_ACTIONS = [
    "read manager memory and local knowledge routes",
    "open the returned source-of-truth files",
    "use focused CRM/Gmail reads when the task requires live state",
]

DEFAULT_FORBIDDEN_ACTIONS = [
    "write to CRM, AutoStop App, Gmail, or files without task-specific owner intent",
    "copy raw CRM records, store orders or stock rows, cashbox ledgers, full repair orders, raw email threads, or secrets into memory or docs",
]

DEFAULT_VERIFICATION = [
    "state which source-of-truth files or live tools were used",
    "state missing required context instead of inventing facts",
]

MEMORY_SOURCES = {
    "local_sqlite": "knowledge_index_and_local_rules",
    "crm_mcp": "operational_memory_and_live_board_context",
    "store_api": "live_store_catalog_stock_orders_quotes_and_marketplace_context",
    "rule": "before CRM or store work, read live focused context; before broad docs, use local knowledge routes",
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
        matched_route = next(
            (route for route in knowledge.get("routes", []) if route.get("domain") == command_route.get("domain")),
            None,
        )
        if matched_route:
            for field in (
                "source_of_truth",
                "reference_files",
                "optional_runtime_files",
                "optional_available_files",
                "optional_missing_files",
                "optional_runtime_available",
                "optional_runtime_note",
            ):
                knowledge[field] = matched_route.get(field, knowledge.get(field))
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
    elif domain == "store_management":
        read_order = STORE_READ_ORDER
        allowed_actions = STORE_ALLOWED_ACTIONS
        forbidden_actions = STORE_FORBIDDEN_ACTIONS
        verification = STORE_VERIFICATION
        next_actions = list(context.get("next_actions") or [])
    elif domain == "store_analytics_reporting":
        read_order = STORE_ANALYTICS_READ_ORDER
        allowed_actions = STORE_ANALYTICS_ALLOWED_ACTIONS
        forbidden_actions = STORE_ANALYTICS_FORBIDDEN_ACTIONS
        verification = STORE_ANALYTICS_VERIFICATION
        next_actions = list(context.get("next_actions") or [])
    elif domain == "knowledge_intake":
        read_order = KNOWLEDGE_HYGIENE_READ_ORDER
        allowed_actions = KNOWLEDGE_HYGIENE_ALLOWED_ACTIONS
        forbidden_actions = KNOWLEDGE_HYGIENE_FORBIDDEN_ACTIONS
        verification = KNOWLEDGE_HYGIENE_VERIFICATION
        next_actions = list(context.get("next_actions") or [])
    elif domain == "remote_codex_access":
        read_order = REMOTE_ACCESS_READ_ORDER
        allowed_actions = REMOTE_ACCESS_ALLOWED_ACTIONS
        forbidden_actions = REMOTE_ACCESS_FORBIDDEN_ACTIONS
        verification = REMOTE_ACCESS_VERIFICATION
        next_actions = list(context.get("next_actions") or [])
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
            "workflow_id": command_route.get("workflow_id") or command_route.get("command_id"),
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
            "required_reads": command_route.get("required_reads", []),
            "write_domains": command_route.get("write_domains", []),
            "external_connectors": command_route.get("external_connectors", []),
            "completion_checks": command_route.get("completion_checks", []),
        },
        "source_boundaries": {
            "crm": "live source of truth for cards, clients, vehicles, repair orders, payments, cashboxes, files, and board state",
            "store": "AutoStop App API is the live source of truth for catalog, stock, batches, storage locations, suppliers, quote requests, internet orders, warehouse operations, and marketplace state",
            "manager_memory": "durable non-CRM context, rules, lessons, tasks, reminders, and short conclusions",
            "gmail": "source of truth for raw email messages, threads, drafts, labels, attachments, and sent history",
            "store_analytics": "AutoStop App aggregate report is the source of truth; raw event rows never enter agent context",
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
