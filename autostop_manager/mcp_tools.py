from __future__ import annotations

from typing import Any

from .storage import ManagerMemoryStore


def register_manager_memory_tools(server: Any, store: ManagerMemoryStore | None = None) -> None:
    memory = store or ManagerMemoryStore()

    @server.tool(
        name="remember",
        description=(
            "Store long-term manager memory that does not belong in AutoStop CRM cards: "
            "facts, agreements, personal matters, rent notes, operating context, or useful experience."
        ),
    )
    def remember(
        content: str,
        kind: str = "note",
        title: str = "",
        category: str = "general",
        source: str = "chatgpt",
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        return memory.remember(
            content,
            kind="fact" if kind == "fact" else "note",
            title=title,
            category=category,
            source=source,
            tags=tags,
        )

    @server.tool(
        name="recall",
        description="Search the manager long-term memory. Use this before assuming owner context is unknown.",
    )
    def recall(query: str = "", limit: int = 20) -> dict[str, Any]:
        return memory.recall(query, limit=limit)

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
        description="Return manager memory context for today's work: due tasks, due reminders, recent journal, and rules.",
    )
    def today_context(limit: int = 20) -> dict[str, Any]:
        return memory.today_context(limit=limit)

    @server.tool(
        name="manager_journal",
        description="Append a short manager journal entry after important decisions or CRM work.",
    )
    def manager_journal(
        event: str,
        source: str = "chatgpt",
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        return memory.journal(event, source=source, tags=tags)
