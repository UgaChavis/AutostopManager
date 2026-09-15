"""CRM-only registration adapter over the existing guarded operation ledger.

The native Manager endpoint never registers these tools. This module has no
learning state, document routing, separate memory, or external execution.
"""

from __future__ import annotations

from collections.abc import Collection
from typing import Any

from .agent_gateway import agent_envelope
from .storage import StoreState


def _workflow_envelope(result: dict[str, Any], *, next_actions: list[str] | None = None) -> dict[str, Any]:
    ok = bool(result.get("ok"))
    run_id = result.get("run_id") or result.get("id")
    return agent_envelope(
        ok=ok,
        status=str(result.get("status") or ("failed" if not ok else "completed")),
        run_id=run_id if isinstance(run_id, int) else None,
        summary=result,
        warnings=[] if ok else [str(result.get("error") or "workflow_operation_failed")],
        next_actions=next_actions or [],
    )


def register_crm_workflow_tools(server: Any, state: StoreState, include_tools: Collection[str]) -> None:
    def tool(**kwargs: Any) -> Any:
        if kwargs["name"] in include_tools:
            return server.tool(**kwargs)
        return lambda function: function

    @tool(
        name="list_agent_workflows",
        description="List workflows actually registered in this CRM server. Exact-name selection only; no keyword routing.",
    )
    def list_agent_workflows(query: str = "", intent: str | None = None, limit: int = 50) -> dict[str, Any]:
        registered = server._tool_manager._tools
        items = [
            {"workflow_id": name, "intent": name, "tool": name}
            for name in sorted(registered)
            if (name.startswith("agent_") and name.endswith("_workflow")) or name == "store_quote_conductor"
        ]
        selected = [item for item in items if item["workflow_id"] == (intent or query)]
        limit = max(1, min(int(limit), 100))
        return agent_envelope(
            ok=True,
            status="completed",
            summary={"items": items[:limit], "workflow_count": len(items), "selected_workflows": selected},
            page={"limit": limit, "returned": len(items[:limit]), "has_more": len(items) > limit},
            meta={"registry": "live_crm_tools"},
        )

    @tool(
        name="agent_bootstrap",
        description="Read unfinished technical operations and available CRM workflows; no business-system reads or learning.",
    )
    def agent_bootstrap(query: str = "", intent: str | None = None, limit: int = 8) -> dict[str, Any]:
        active = state.list_active_manager_runs(limit=max(1, min(int(limit), 100)))
        workflows = list_agent_workflows(query=query, intent=intent)
        return agent_envelope(
            ok=bool(active.get("ok")),
            status="ready" if active.get("ok") else "failed",
            summary={
                "role": "AutoStop customer operations",
                "intent": intent,
                "selected_workflows": workflows["summary"]["selected_workflows"],
                "unfinished_runs": [
                    {"run_id": item["id"], **{k: v for k, v in item.items() if k != "id"}}
                    for item in active.get("items", [])
                ],
                "source_boundaries": {
                    "live_data": ["CRM", "Store", "Gmail", "Telegram"],
                    "local_state": "Technical checkpoints and idempotency only; no correspondence or business records.",
                },
            },
            warnings=[] if active.get("ok") else [str(active.get("error") or "ledger_read_failed")],
            meta={"workflow_registry_tool": "list_agent_workflows", "action_contract_tool": "prepare_action_contract"},
        )

    @tool(
        name="start_workflow",
        description=(
            "Start an idempotent Agent Gateway v2 workflow in planned state. This records compact scope/refs only and does not call CRM or Gmail."
        ),
    )
    def start_workflow_tool(
        workflow_id: str,
        intent: str,
        idempotency_key: str,
        query: str = "",
        request_id: str = "",
        correlation_id: str = "",
        actor: str = "codex-owner-agent",
        scope: dict[str, Any] | None = None,
        selected_ids: list[str] | None = None,
        dry_run: bool = False,
        source: str = "codex",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        result = state.start_workflow_run(
            workflow_id=workflow_id,
            intent=intent,
            query=query,
            request_id=request_id,
            idempotency_key=idempotency_key,
            correlation_id=correlation_id,
            actor=actor,
            scope=scope,
            selected_ids=selected_ids,
            dry_run=dry_run,
            source=source,
            metadata=metadata,
        )
        return _workflow_envelope(result, next_actions=["workflow_transition to executing after automatic preflight"])

    @tool(
        name="workflow_status",
        description="Read one compact workflow state, checkpoint, events, and external connector step references.",
    )
    def workflow_status_tool(
        run_id: int,
        include_events: bool = False,
        include_external_steps: bool = True,
    ) -> dict[str, Any]:
        result = state.get_manager_run(
            run_id,
            include_events=include_events,
            include_external_steps=include_external_steps,
        )
        if result.get("ok"):
            item = result.get("item", {})
            return agent_envelope(
                ok=True,
                status=str(item.get("status") or "completed"),
                run_id=run_id,
                summary=item,
            )
        return _workflow_envelope(result)

    @tool(
        name="workflow_transition",
        description=(
            "Advance a workflow through planned, executing, external_wait, verifying, compensating, and terminal states "
            "using strict transitions and expected_state_version compare-and-swap. Completed requires positive evidence "
            "and rejects explicit executor or verification failure markers."
        ),
    )
    def workflow_transition_tool(
        run_id: int,
        status: str,
        message: str = "",
        verification: dict[str, Any] | None = None,
        summary: str = "",
        expected_state_version: int | None = None,
    ) -> dict[str, Any]:
        return _workflow_envelope(
            state.transition_workflow_run(
                run_id,
                status=status,
                message=message,
                verification=verification,
                summary=summary,
                expected_state_version=expected_state_version,
            )
        )

    @tool(
        name="workflow_checkpoint",
        description=(
            "Persist a compact resumable checkpoint and selected IDs with expected_state_version compare-and-swap. "
            "Raw CRM dumps and email bodies are rejected."
        ),
    )
    def workflow_checkpoint_tool(
        run_id: int,
        checkpoint: dict[str, Any],
        selected_ids: list[str] | None = None,
        message: str = "",
        expected_state_version: int | None = None,
    ) -> dict[str, Any]:
        return _workflow_envelope(
            state.checkpoint_workflow_run(
                run_id,
                checkpoint=checkpoint,
                selected_ids=selected_ids,
                message=message,
                expected_state_version=expected_state_version,
            )
        )

    @tool(
        name="workflow_wait_for_external",
        description=(
            "Register a refs-only step for a separate connector such as Gmail or Telegram and move the workflow to external_wait. "
            "Use expected_state_version compare-and-swap; message bodies, snippets, and raw content are rejected."
        ),
    )
    def workflow_wait_for_external_tool(
        run_id: int,
        step_id: str,
        connector: str,
        action: str,
        request_refs: dict[str, Any] | None = None,
        expected_state_version: int | None = None,
    ) -> dict[str, Any]:
        return _workflow_envelope(
            state.register_external_step(
                run_id,
                step_id=step_id,
                connector=connector,
                action=action,
                request_refs=request_refs,
                expected_state_version=expected_state_version,
            ),
            next_actions=["call the separate connector", "complete_external_step with result IDs only"],
        )

    @tool(
        name="complete_external_step",
        description=(
            "Complete one external connector step with message/thread/draft/attachment/file IDs and timestamps only. "
            "Use expected_state_version compare-and-swap and never store raw Gmail or Telegram content in the manager ledger."
        ),
    )
    def complete_external_step_tool(
        run_id: int,
        step_id: str,
        result_refs: dict[str, Any] | None = None,
        expected_state_version: int | None = None,
    ) -> dict[str, Any]:
        return _workflow_envelope(
            state.complete_external_step(
                run_id,
                step_id=step_id,
                result_refs=result_refs,
                expected_state_version=expected_state_version,
            ),
            next_actions=["workflow_resume after all external steps are complete"],
        )

    @tool(
        name="workflow_resume",
        description=(
            "Resume a planned or externally-waiting workflow from its compact checkpoint with expected_state_version "
            "compare-and-swap; refuses while external steps remain pending."
        ),
    )
    def workflow_resume_tool(run_id: int, expected_state_version: int | None = None) -> dict[str, Any]:
        return _workflow_envelope(state.resume_workflow_run(run_id, expected_state_version=expected_state_version))

    @tool(
        name="workflow_cancel",
        description=(
            "Cancel a non-terminal workflow with expected_state_version compare-and-swap without changing CRM, Gmail, or Telegram state."
        ),
    )
    def workflow_cancel_tool(
        run_id: int,
        reason: str = "",
        expected_state_version: int | None = None,
    ) -> dict[str, Any]:
        return _workflow_envelope(
            state.cancel_workflow_run(
                run_id,
                reason=reason,
                expected_state_version=expected_state_version,
            )
        )
