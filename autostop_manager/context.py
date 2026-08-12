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
    "CRM, AutoStop App and Gmail are the source of truth for their own business data; Manager stores only durable routing, rules and compact conclusions.",
    "Never persist raw CRM/Store/Gmail payloads, client or vehicle identifiers, money ledgers, credentials or secrets in Manager memory or docs.",
    "For any write: resolve the exact target, reread current state, use the guarded dry-run/apply path, then reread and verify.",
]

DEFAULT_POLICY: dict[str, list[str]] = {
    "hot_rules": [],
    "read_order": [
        "open the returned open_first/source_of_truth files",
        "use focused source reads before broad exports",
    ],
    "allowed_actions": [
        "read Manager memory and routed local knowledge",
        "use focused live reads required by the task",
    ],
    "forbidden_actions": [
        "write business or external state without task-specific owner intent",
        "copy raw business data or secrets into Manager memory, docs or Git",
    ],
    "verification": [
        "state the source used and any missing required context",
        "reread every changed target",
    ],
}

DOMAIN_POLICIES: dict[str, dict[str, list[str]]] = {
    "board_cleanup_autopilot": {
        "hot_rules": [
            "For 'Приберись', inspect vehicle passport and client data first; phone is the primary client match key.",
            "Keep title, vehicle, description, board_summary and at most three tags concise; leave an empty description empty.",
            "Routine cleanup never moves or archives cards and never changes repair orders or payments without separate exact authorization.",
        ],
        "read_order": [
            "agent_bootstrap and agent_board_digest",
            "manager_board_scan dry_run, then focused agent_search and agent_entity_context",
            "audit_client_links when client data is incomplete or ambiguous",
        ],
        "allowed_actions": [
            "cleanup_card dry_run/apply for confirmed card, structured vehicle/client and board_summary deltas",
            "execute a direct safe card task and write back only its compact supported result",
        ],
        "forbidden_actions": [
            "move, archive, delete or merge records during routine cleanup",
            "overwrite operator evidence or change payments, repair-order lines, totals, deadlines or indicators without exact authorization",
            "leave phone, VIN, plate or raw diagnostics in public descriptions or board_summary",
        ],
        "verification": [
            "reread every written card and verify board_summary_stale=false",
            "verify touched client links and report moved, archived, repair-order and payment counts",
        ],
    },
    "store_management": {
        "hot_rules": [
            "AutoStop App API is authoritative; general reads are redacted and exact full quotes are transient only.",
            "Use stateless Store bootstrap for health and store_digest for owner-visible changes.",
            "Named inventory workflows cover common writes; all other employee actions require guarded store_owner_api and the live schema.",
        ],
        "read_order": [
            "agent_bootstrap for a stateless Store readiness snapshot or agent_board_digest(scope=store)",
            "agent_search and agent_entity_context for exact store entities; full quotes use the dedicated quote credential transiently",
            "prepare_action_contract, named workflow or store_owner_capabilities/store_owner_api for writes",
        ],
        "allowed_actions": [
            "read bounded catalog, stock, quote, store_sourcing_offer, order, supplier, warehouse and marketplace state",
            "append a note or perform another named Store operation when exactly authorized",
            "apply exact authorized writes with revision, idempotency, dry-run and readback",
        ],
        "forbidden_actions": [
            "read the Store database or use legacy side-effecting GET routes",
            "persist raw Store payloads or perform writes outside a named workflow or guarded store_owner_api",
        ],
        "verification": [
            "advance digest cursors only after the final page",
            "keep unverified applies compensating and retain compact refs only",
        ],
    },
    "store_analytics_reporting": {
        "hot_rules": [
            "Use only the aggregate Store analytics report; raw visitor or event rows never enter agent context."
        ],
        "read_order": [
            "open docs/agent/store_analytics_playbook.md",
            "discover and call get_store_analytics_report for the requested period",
        ],
        "allowed_actions": ["report aggregate trends, rankings, funnel and previous-period comparison"],
        "forbidden_actions": [
            "request or persist raw analytics events, visitor/session identifiers or private form contents"
        ],
        "verification": ["verify store_analytics_report_v1, aggregatedOnly=true and rawEventsIncluded=false"],
    },
    "knowledge_intake": {
        "hot_rules": [
            "Keep one canonical owner per rule; migrate unique active content before deleting obsolete text.",
            "Run cleanup-audit before deletion and all knowledge audits after routing changes.",
        ],
        "read_order": [
            "open docs/agent/knowledge_shelves.md and knowledge-probe results",
            "inventory tracked files, references and runtime dependencies before cleanup-audit",
        ],
        "allowed_actions": ["update canonical docs/routes and remove proven obsolete or generated files"],
        "forbidden_actions": [
            "delete an active source-of-truth file or unique instruction",
            "change CRM, Store, Gmail or remote-PC business state during documentation cleanup",
        ],
        "verification": ["run knowledge, annotation, skill, cleanup, test, lint and Git-diff checks"],
    },
    "remote_codex_access": {
        "hot_rules": [
            "Open docs/agent/codex_home_pc_reverse_ssh.md; for FST.KZ first read /root/.codex/CODEX_VPN_FST_ACCESS.md.",
            "Resolve the exact host and stop on any SSH host-key mismatch.",
        ],
        "read_order": [
            "open the target-specific route and run bounded identity/status checks",
            "for managed-pc resolve the exact alias and run status before an operation",
        ],
        "allowed_actions": ["operate only the exact owner-authorized server or PC needed by the task"],
        "forbidden_actions": [
            "print private keys, passwords, secrets or VPN profiles; bypass host-key checks or mix route credentials",
            "reboot, mass-delete or stop critical services without a separate exact instruction",
        ],
        "verification": ["report the exact route used and rerun its health/status check after changes"],
    },
    "automotive_repair": {
        "hot_rules": [
            "Choose CRM, Store, VIN/OEM, official, licensed and public sources adaptively for the exact question.",
            "Forums are hypotheses; exact fitment, safety, procedure, torque, fluids and programming need appropriate vehicle-specific authority.",
        ],
        "read_order": [
            "open docs/agent/automotive_repair_source_playbook.md",
            "identify vehicle, unit and requested fact, then read only relevant sources",
        ],
        "allowed_actions": [
            "read focused CRM or AutoStop App vehicle/store context only when the request requires it",
            "combine applicable evidence and report confidence and missing context",
        ],
        "forbidden_actions": [
            "bypass access controls or copy licensed manuals",
            "present public recall metadata as VIN-specific status",
            "write CRM, Store or Gmail unless separately requested",
        ],
        "verification": ["distinguish source types and verify exact vehicle/unit applicability"],
    },
}

LONG_RUN_CONTEXT_SAFETY = {
    "rules": [
        "Use Gateway workflow checkpoints for multi-operation work; prefer focused reads and small verified batches over raw dumps.",
        "Keep raw board snapshots, client identifiers and full repair orders out of chat and durable Manager state.",
    ],
    "checkpoint_event_types": ["planned_action", "checkpoint", "skip", "write", "risk", "verification"],
    "recovery": [
        "Use agent_bootstrap, workflow_status and workflow_resume for the newest unfinished workflow.",
        "Resume external waits only through complete_external_step with the latest state_version.",
    ],
}

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
    policy = DOMAIN_POLICIES.get(str(domain or ""), DEFAULT_POLICY)
    rules = [*GENERAL_HOT_RULES, *policy.get("hot_rules", [])]
    return rules[: max(1, min(limit, 8))]


def _select_store_operation(query: str, selection: object) -> dict[str, object] | None:
    if not isinstance(selection, dict):
        return None
    lowered = str(query or "").casefold()
    matches: list[tuple[int, str, dict[str, object]]] = []
    for operation, raw_contract in selection.items():
        if not isinstance(operation, str) or not isinstance(raw_contract, dict):
            continue
        aliases = raw_contract.get("aliases")
        if not isinstance(aliases, list):
            continue
        for alias in aliases:
            normalized = str(alias or "").casefold()
            if normalized and normalized in lowered:
                matches.append((len(normalized), operation, raw_contract))
    if not matches:
        return None
    _, operation, contract = max(matches, key=lambda item: (item[0], item[1]))
    return {"operation": operation, **contract}


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
    has_actionable_knowledge = bool(knowledge.get("has_knowledge")) or bool(command_route)
    domain = str(
        (knowledge.get("best_domain") if has_actionable_knowledge else None) or command_route.get("domain") or ""
    )

    policy = DOMAIN_POLICIES.get(domain, DEFAULT_POLICY)
    read_order = list(policy["read_order"])
    allowed_actions = list(policy["allowed_actions"])
    forbidden_actions = list(policy["forbidden_actions"])
    verification = list(policy["verification"])
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
            "open_first": knowledge.get("open_first") if has_actionable_knowledge else None,
            "source_of_truth": knowledge.get("source_of_truth", []) if has_actionable_knowledge else [],
            "reference_files": knowledge.get("reference_files", []) if has_actionable_knowledge else [],
            "optional_runtime_files": knowledge.get("optional_runtime_files", []) if has_actionable_knowledge else [],
            "optional_available_files": knowledge.get("optional_available_files", [])
            if has_actionable_knowledge
            else [],
            "optional_missing_files": knowledge.get("optional_missing_files", []) if has_actionable_knowledge else [],
            "optional_runtime_available": knowledge.get("optional_runtime_available", False)
            if has_actionable_knowledge
            else False,
            "optional_runtime_note": knowledge.get("optional_runtime_note", "") if has_actionable_knowledge else "",
            "confidence": knowledge.get("confidence"),
            "required_reads": command_route.get("required_reads", []),
            "write_domains": command_route.get("write_domains", []),
            "external_connectors": command_route.get("external_connectors", []),
            "completion_checks": command_route.get("completion_checks", []),
            "read_entity_selection": command_route.get("read_entity_selection", {}),
            "operation_selection": command_route.get("operation_selection", {}),
            "selected_operation": _select_store_operation(
                str(context.get("query") or ""),
                command_route.get("operation_selection", {}),
            ),
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
