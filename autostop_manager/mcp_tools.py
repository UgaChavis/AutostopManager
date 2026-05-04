from __future__ import annotations

from typing import Any

from .fluid_maintenance import build_fluid_maintenance_plan
from .knowledge_base import audit_knowledge_base, probe_knowledge_base, search_knowledge_base, sync_knowledge_base
from .service_management import build_service_management_plan
from .source_catalog import recommend_automotive_sources
from .storage import ManagerMemoryStore
from .vin_lookup import lookup_original_parts


def register_manager_memory_tools(server: Any, store: ManagerMemoryStore | None = None) -> None:
    memory = store or ManagerMemoryStore()

    @server.tool(
        name="remember",
        description=(
            "Store long-term manager memory that does not belong in AutoStop CRM cards: "
            "facts, agreements, personal matters, rent notes, operating context, durable conclusions from approved source files, or useful experience."
        ),
    )
    def remember(
        content: str,
        kind: str = "note",
        title: str = "",
        category: str = "general",
        source: str = "chatgpt",
        tags: list[str] | None = None,
        confidence: float = 1.0,
    ) -> dict[str, Any]:
        return memory.remember(
            content,
            kind="fact" if kind == "fact" else "note",
            title=title,
            category=category,
            source=source,
            tags=tags,
            confidence=confidence,
        )

    @server.tool(
        name="recall",
        description=(
            "Search the manager long-term memory with relevance scoring and optional kind/category/tag filters. "
            "Use this before assuming owner context, style preferences, operating lessons, or durable rules are unknown."
        ),
    )
    def recall(
        query: str = "",
        limit: int = 20,
        kind: str | None = None,
        category: str | None = None,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        return memory.recall(query, limit=limit, kind=kind, category=category, tags=tags)

    @server.tool(
        name="learn_from_feedback",
        description=(
            "Store a concise reusable lesson when owner feedback, praise, criticism, clear success, or clear failure "
            "should improve future manager behavior. Store the lesson, not CRM/Gmail/raw event copies."
        ),
    )
    def learn_from_feedback(
        content: str,
        title: str = "",
        applies_to: str = "general",
        signal: str = "manager_observation",
        recommendation: str = "",
        avoid: str = "",
        importance: float = 0.5,
        confidence: float = 0.7,
        source: str = "chatgpt",
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        return memory.learn_from_feedback(
            content,
            title=title,
            applies_to=applies_to,
            signal=signal,
            recommendation=recommendation,
            avoid=avoid,
            importance=importance,
            confidence=confidence,
            source=source,
            tags=tags,
        )

    @server.tool(
        name="recall_lessons",
        description="Search reusable manager lessons by task text, applies_to, signal, and tags before similar work.",
    )
    def recall_lessons(
        query: str = "",
        limit: int = 20,
        applies_to: str | None = None,
        signal: str | None = None,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        return memory.recall_lessons(query, limit=limit, applies_to=applies_to, signal=signal, tags=tags)

    @server.tool(
        name="memory_map",
        description="Return compact counts and sections for manager memory so the agent can navigate memory before broad recall.",
    )
    def memory_map() -> dict[str, Any]:
        return memory.memory_map()

    @server.tool(
        name="memory_topics",
        description="Return memory categories and tags with counts and examples for navigation and review.",
    )
    def memory_topics(examples_limit: int = 3) -> dict[str, Any]:
        return memory.memory_topics(examples_limit=examples_limit)

    @server.tool(
        name="memory_context_for",
        description=(
            "Build compact memory context for a task: relevant preferences, rules, lessons, source boundaries, and suggested use. "
            "Use as context for judgment, not as a rigid text template."
        ),
    )
    def memory_context_for(task: str, limit: int = 5) -> dict[str, Any]:
        return memory.memory_context_for(task, limit=limit)

    @server.tool(
        name="memory_gaps",
        description="Return sparse or empty memory areas and review prompts without copying CRM or Gmail data.",
    )
    def memory_gaps() -> dict[str, Any]:
        return memory.memory_gaps()

    @server.tool(
        name="add_manager_task",
        description="Add a manager-level task that is not a CRM vehicle card or repair order.",
    )
    def add_manager_task(
        title: str,
        details: str = "",
        due_at: str | None = None,
        source: str = "chatgpt",
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        return memory.add_task(title, details=details, due_at=due_at, source=source, tags=tags)

    @server.tool(
        name="today_context",
        description="Return manager memory context for today's work: due tasks, due reminders, recent journal, rules, and reusable routing context.",
    )
    def today_context(limit: int = 20) -> dict[str, Any]:
        return memory.today_context(limit=limit)

    @server.tool(
        name="manager_journal",
        description="Append a short manager journal entry after important decisions, source changes, file intake, or CRM work.",
    )
    def manager_journal(
        event: str,
        source: str = "chatgpt",
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        return memory.journal(event, source=source, tags=tags)

    @server.tool(
        name="sync_knowledge_base",
        description=(
            "Index docs/agent/knowledge_map.json, routed playbooks, source catalogs, and model-specific skills into SQLite "
            "so the manager can navigate local knowledge without reading every file."
        ),
    )
    def sync_knowledge_base_tool() -> dict[str, Any]:
        return sync_knowledge_base(memory)

    @server.tool(
        name="probe_knowledge_base",
        description=(
            "Cheaply check whether the local knowledge base has relevant knowledge for a vehicle, brand, model, system, or task. "
            "Use this before broad search or full document reads; if has_knowledge is true, open the returned source_of_truth/open_first route first."
        ),
    )
    def probe_knowledge_base_tool(
        query: str,
        limit: int = 5,
    ) -> dict[str, Any]:
        return probe_knowledge_base(memory, query, limit=limit)

    @server.tool(
        name="search_knowledge_base",
        description=(
            "Search the indexed AutostopManager knowledge base by query and optional domain. "
            "Use before broad file reads for diagnostics, fluids, VIN/OEM, parts, CRM management, or model-specific knowledge."
        ),
    )
    def search_knowledge_base_tool(
        query: str,
        domain: str | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        return search_knowledge_base(memory, query, domain=domain, limit=limit)

    @server.tool(
        name="audit_knowledge_base",
        description=(
            "Audit docs/agent/knowledge_map.json, compact route cards, mapped source files, and SQLite index counts. "
            "Use after knowledge intake or when local knowledge routing looks stale."
        ),
    )
    def audit_knowledge_base_tool() -> dict[str, Any]:
        return audit_knowledge_base(memory)

    @server.tool(
        name="lookup_original_parts",
        description=(
            "Classify a VIN, chassis number, or market code and return a source-aware lookup plan for original catalog numbers."
        ),
    )
    def lookup_original_parts_tool(
        identifier: str,
        model_year: int | None = None,
        make_hint: str | None = None,
    ) -> dict[str, Any]:
        return lookup_original_parts(identifier, model_year=model_year, make_hint=make_hint)

    @server.tool(
        name="recommend_automotive_sources",
        description=(
            "Recommend authoritative repair, TSB, recall, diagnostic, wiring, labor, fluid, torque, or OEM source routes "
            "by brand and data type without copying licensed source content."
        ),
    )
    def recommend_automotive_sources_tool(
        brand: str | None = None,
        data_type: str | None = None,
        include_licensed: bool = True,
        limit: int = 10,
    ) -> dict[str, Any]:
        return recommend_automotive_sources(
            brand=brand,
            data_type=data_type,
            include_licensed=include_licensed,
            limit=limit,
        )

    @server.tool(
        name="recommend_fluid_maintenance_sources",
        description=(
            "Build a source-backed plan for oils, operating fluids, fill capacities, and ТО fluid service by vehicle unit. "
            "Use before giving engine, transmission, differential, transfer case, brake, coolant, or steering fluid facts."
        ),
    )
    def recommend_fluid_maintenance_sources_tool(
        brand: str | None = None,
        unit: str | None = None,
        vin: str | None = None,
        chassis: str | None = None,
        model: str | None = None,
        year: int | None = None,
        engine_code: str | None = None,
        transmission_code: str | None = None,
        drivetrain: str | None = None,
        market: str | None = None,
        include_licensed: bool = True,
        limit: int = 10,
    ) -> dict[str, Any]:
        return build_fluid_maintenance_plan(
            brand=brand,
            unit=unit,
            vin=vin,
            chassis=chassis,
            model=model,
            year=year,
            engine_code=engine_code,
            transmission_code=transmission_code,
            drivetrain=drivetrain,
            market=market,
            include_licensed=include_licensed,
            limit=limit,
        )

    @server.tool(
        name="recommend_service_management_actions",
        description=(
            "Build a Krasnoyarsk AutoStop/Автоспорт workshop-management action plan for parts procurement, "
            "repair triage, staff load, customer flow, finance control, daily CRM control, or knowledge intake."
        ),
    )
    def recommend_service_management_actions_tool(
        area: str | None = None,
        city: str = "Красноярск",
        vehicle: str | None = None,
        vin: str | None = None,
        chassis: str | None = None,
        part_number: str | None = None,
        part_name: str | None = None,
        urgency: str | None = None,
        role: str | None = None,
        complaint: str | None = None,
        dtc_or_scan: str | None = None,
        engine: str | None = None,
        transmission: str | None = None,
        mileage: str | None = None,
        current_load: str | None = None,
        output_or_hours: str | None = None,
        quality_signal: str | None = None,
        card_id: str | None = None,
        client_contact: str | None = None,
        next_action: str | None = None,
        approval_status: str | None = None,
        repair_orders: str | None = None,
        cashbox: str | None = None,
        payment_status: str | None = None,
        file_path: str | None = None,
        source_type: str | None = None,
        license_status: str | None = None,
        target_playbook: str | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        return build_service_management_plan(
            area=area,
            city=city,
            vehicle=vehicle,
            vin=vin,
            chassis=chassis,
            part_number=part_number,
            part_name=part_name,
            urgency=urgency,
            role=role,
            complaint=complaint,
            dtc_or_scan=dtc_or_scan,
            engine=engine,
            transmission=transmission,
            mileage=mileage,
            current_load=current_load,
            output_or_hours=output_or_hours,
            quality_signal=quality_signal,
            card_id=card_id,
            client_contact=client_contact,
            next_action=next_action,
            approval_status=approval_status,
            repair_orders=repair_orders,
            cashbox=cashbox,
            payment_status=payment_status,
            file_path=file_path,
            source_type=source_type,
            license_status=license_status,
            target_playbook=target_playbook,
            limit=limit,
        )
