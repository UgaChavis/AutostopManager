from __future__ import annotations

from typing import Any

from .knowledge_base import plan_command_routes, probe_knowledge_base
from .storage import ManagerMemoryStore, load_manager_rules


DOMAIN_REQUIRED_CONTEXT_DEFAULTS: dict[str, list[str]] = {
    "automotive_repair": [
        "VIN or chassis",
        "market",
        "engine code",
        "transmission code",
        "module and DTC or scan data",
    ],
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

EFFECT_POLICIES: dict[str, dict[str, list[str]]] = {
    "crm_write": {
        "hot_rules": ["CRM writes require an exact target, current revision, idempotency and proof-bound apply."],
        "read_order": ["reread the exact CRM target", "prepare action contract, dry-run, then apply"],
        "allowed_actions": ["apply only the task-scoped CRM diff"],
        "forbidden_actions": ["change unrelated CRM fields, totals, payments, deadlines or columns"],
        "verification": ["reread the exact changed target and reconcile the planned diff"],
    },
    "store_write": {
        "hot_rules": [
            "Store writes require one exact live target, current revision, a reviewed operation contract, idempotency and proof-bound apply."
        ],
        "read_order": [
            "reread the exact Store target and current operation schema",
            "prepare action contract, dry-run, then apply",
        ],
        "allowed_actions": ["apply only the task-scoped Store diff"],
        "forbidden_actions": [
            "publish externally, procure, pay, discount, delete or change another Store target without the corresponding explicit effect"
        ],
        "verification": ["reread the exact Store target and reconcile every planned field"],
    },
    "document": {
        "hot_rules": ["Generate from the current source and pass render, totals and attachment-hash QA."],
        "read_order": ["reread the source record and current document schema"],
        "allowed_actions": ["generate the requested document from verified source data"],
        "forbidden_actions": ["send or retain a document that failed QA"],
        "verification": ["verify document_guard, rendered totals and SHA-256"],
    },
    "external_send": {
        "hot_rules": [
            "External visibility or delivery requires an exact destination or target, an authorized channel or principal, and immutable content or attachment identity when applicable."
        ],
        "read_order": ["reread the target, active channel schema, and content identity"],
        "allowed_actions": ["publish or send once through a proof-bound idempotent operation"],
        "forbidden_actions": ["publish or send when target, channel, content, QA or attachment identity is ambiguous"],
        "verification": ["record compact refs and verify exactly one customer-visible or outbound result"],
    },
    "account_auth": {
        "hot_rules": [
            "Account authorization requires one explicitly selected account and interactive owner-controlled login without exposing credentials or session material."
        ],
        "read_order": ["check the exact selected account service and current authorization state"],
        "allowed_actions": ["run only the selected account's reviewed interactive authorization flow"],
        "forbidden_actions": ["send messages, copy sessions, expose login secrets or authorize a different account"],
        "verification": ["probe the same account for authorized=true and require the expected session identity"],
    },
    "remote_diagnostics": {
        "hot_rules": ["Tablet calls require an explicit live-session owner start and the PAD VII current status gate."],
        "read_order": [
            "read the Manager PAD VII playbook and server contract",
            "wait for metrics-confirmed READY and current status gate",
            "take a fresh screenshot-free observation before a single permitted action",
        ],
        "allowed_actions": ["use only the isolated typed diagnostics MCP under the one-observe/one-action contract"],
        "forbidden_actions": [
            "call tablet tools before live owner authority, reuse an observation, retry unknown outcomes or retain raw evidence",
            "clear DTCs, run active tests, reset, code, adapt, calibrate, flash or perform immobilizer work without exact owner authority",
        ],
        "verification": [
            "confirm every screen result by an explicit fresh observation and summarize only safe conclusions"
        ],
    },
    "finance": {
        "hot_rules": ["Financial effects require direct task-specific owner intent and proof-bound apply."],
        "read_order": ["reread the current monetary and tax basis for the exact business target before preview"],
        "allowed_actions": ["apply only the owner-authorized financial result"],
        "forbidden_actions": ["apply a monetary or tax mismatch without separate confirmation"],
        "verification": ["reconcile the exact business target's monetary basis, tax status and resulting state"],
    },
    "destructive": {
        "hot_rules": ["Resolve exact targets and recovery material before a destructive action."],
        "read_order": ["inspect references, runtime dependencies and recovery path"],
        "allowed_actions": ["perform only the explicitly scoped destructive change"],
        "forbidden_actions": ["use broad targets, force-push, blind reset or unverified deletion"],
        "verification": ["verify recovery remains possible and no extra target changed"],
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
    del store
    query = (query or "").strip()
    limit = max(1, min(limit, 50))

    command_routes = plan_command_routes(query, intent=intent)
    command_route = command_routes[0] if command_routes else None
    preferred_domains = list(
        dict.fromkeys(domain for route in command_routes for domain in route.get("knowledge_domains", []) if domain)
    )
    knowledge = probe_knowledge_base(
        None,
        query,
        limit=max(limit, min(len(preferred_domains) + 2, 20)),
        preferred_domains=preferred_domains,
    )
    selected_domains = list(dict.fromkeys([*preferred_domains, knowledge.get("best_domain")]))
    navigation = {str(route.get("domain")): route for route in knowledge.get("routes", [])}
    required_context = list(
        dict.fromkeys(
            item
            for domain in selected_domains
            if domain
            for item in (
                navigation.get(str(domain), {}).get("required_context")
                or DOMAIN_REQUIRED_CONTEXT_DEFAULTS.get(str(domain), [])
            )
        )
    )
    missing_context = [item for item in required_context if not _query_has_context(query, item)]

    return {
        "ok": True,
        "query": query,
        "intent": intent or (command_route or {}).get("intent"),
        "command_route": command_route,
        "command_routes": command_routes,
        "selected_workflows": [route.get("workflow_id") for route in command_routes],
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
        "relevant_memory": [],
        "required_context": required_context,
        "missing_context": missing_context,
        "next_actions": ["execute_workflow_plan"] if command_routes else ["safe_exploration"],
    }


def _policy_for_effects(effects: list[str]) -> dict[str, list[str]]:
    policy = {key: list(value) for key, value in DEFAULT_POLICY.items()}
    for effect in effects:
        extra = EFFECT_POLICIES.get(effect, {})
        for key in policy:
            policy[key].extend(extra.get(key, []))
    return {key: list(dict.fromkeys(value)) for key, value in policy.items()}


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
    first = _route_step(context.get("command_route") or {}, knowledge) if steps else {}
    domain = str(first.get("domain") or "")
    effects = list(dict.fromkeys(effect for step in steps for effect in step.get("effects", [])))
    policy = _policy_for_effects(effects)
    read_order = list(policy["read_order"])
    allowed_actions = list(policy["allowed_actions"])
    forbidden_actions = list(policy["forbidden_actions"])
    verification = list(policy["verification"])
    next_actions = list(context.get("next_actions") or [])
    candidates = [
        {
            key: candidate.get(key)
            for key in ("workflow_id", "domain", "open_first", "confidence", "uncertainty", "matching_terms")
        }
        for candidate in sorted(
            command_routes or knowledge.get("routes", []),
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
    effect_hot_rules = list(policy.get("hot_rules", []))
    hot_rules = [*effect_hot_rules, *canonical_rules[: max(0, 8 - len(effect_hot_rules))]]

    return {
        "ok": True,
        "format": "agent_brief_v1",
        "query": context.get("query"),
        "intent": context.get("intent"),
        "role": "AutoStop operations director agent",
        "language": "ru",
        "answer_style": "short, practical, direct",
        "memory_sources": MEMORY_SOURCES,
        "route": {
            "command_id": first.get("command_id"),
            "workflow_id": first.get("workflow_id"),
            "selected_workflows": [step.get("workflow_id") for step in steps],
            "steps": steps,
            "domain": domain or None,
            "open_first": first.get("open_first"),
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
            "confidence": first.get("confidence") if steps else min(float(knowledge.get("confidence") or 0), 0.49),
            "uncertainty": first.get("uncertainty")
            if steps
            else round(1.0 - min(float(knowledge.get("confidence") or 0), 0.49), 2),
            "selection_mode": "recommended" if steps else "explore",
            "candidates": candidates,
            "required_reads": source_of_truth,
            "write_domains": write_domains,
            "external_connectors": external_connectors,
            "completion_checks": verification,
            "read_entity_selection": {},
            "operation_selection": {},
            "selected_operation": None,
        },
        "source_boundaries": {
            "crm": "live source of truth for cards, clients, vehicles, repair orders, payments, cashboxes, files, and board state",
            "store": "AutoStop App API is the live source of truth for catalog, stock, batches, storage locations, supplier-sourcing evidence, quote requests, internet orders, warehouse operations, and marketplace state",
            "manager_memory": "durable non-business context, rules, lessons, tasks, and short conclusions",
            "gmail": "source of truth for raw email messages, threads, drafts, labels, attachments, and sent history",
            "telegram": "source of truth for raw dialogs, contacts, messages, and media",
            "store_analytics": "AutoStop App aggregate report is the source of truth; raw event rows never enter agent context",
        },
        "hot_rules": hot_rules,
        "read_order": read_order,
        "allowed_actions": allowed_actions,
        "forbidden_actions": forbidden_actions,
        "required_context": context.get("required_context", []),
        "missing_context": context.get("missing_context", []),
        "context_safety": LONG_RUN_CONTEXT_SAFETY,
        "next_actions": next_actions,
        "verification": verification,
    }
