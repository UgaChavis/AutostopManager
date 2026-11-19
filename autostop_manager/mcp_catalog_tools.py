"""Portable Store tool registration shared by full Manager and local catalog labs."""

from __future__ import annotations

import hashlib
from typing import Any

from mcp.types import ToolAnnotations

from .action_contract import prepare_action_contract
from .store_integration import StoreIntegration
from .store_owner_api import StoreOwnerApiClient


def register_store_owner_tools(server: Any, store_owner_client: StoreOwnerApiClient) -> None:
    @server.tool(
        name="store_owner_capabilities",
        description=(
            "READ_ONLY RAW_CAPABILITY: List owner-scoped Store OpenAPI operations or describe one "
            "validation-only input contract; never return business data."
        ),
        annotations=ToolAnnotations(
            title="Store Owner Capabilities",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    def store_owner_capabilities_tool(
        query: str = "",
        limit: int = 200,
        operation_id: str = "",
    ) -> dict[str, Any]:
        return store_owner_client.list_capabilities(
            query=query,
            limit=limit,
            operation_id=operation_id,
        )

    @server.tool(
        name="store_owner_api",
        description=(
            "OWNER_SCOPED RAW_CAPABILITY: Invoke one typed Store operation. Writes require the exact "
            "target and revision, ActionContractV2, idempotency, dry-run proof and reread; results stay transient."
        ),
        annotations=ToolAnnotations(
            title="Store Owner API",
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    def store_owner_api_tool(
        operation_id: str,
        mode: str = "dry_run",
        target_id: str = "",
        path_parameters: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
        body: Any = None,
        form: dict[str, Any] | None = None,
        files: list[dict[str, Any]] | None = None,
        owner_intent: str = "",
        idempotency_key: str = "",
        correlation_id: str = "",
        expected_revision: str | None = None,
        expected_contract_id: str | None = None,
        prepare_for_mode: str = "dry_run",
        dry_run_proof: str | None = None,
        allow_binary_response: bool = False,
    ) -> dict[str, Any]:
        normalized_mode = str(mode or "").strip().casefold()
        revision_options = {"revision_only": True} if normalized_mode == "revision" else {}
        prepared = store_owner_client.prepare_invocation(
            operation_id=operation_id,
            path_parameters=path_parameters,
            query=query,
            body=body,
            form=form,
            files=files,
            expected_revision=expected_revision,
            **revision_options,
        )
        if not prepared.get("ok"):
            return prepared
        raw_capability = prepared.get("summary")
        capability: dict[str, Any] = raw_capability if isinstance(raw_capability, dict) else {}
        method = str(capability.get("method") or "").upper()
        normalized_prepare_for_mode = str(prepare_for_mode or "").strip().casefold()
        if normalized_mode == "prepare" and normalized_prepare_for_mode not in {
            "dry_run",
            "apply",
        }:
            return {
                "ok": False,
                "format": "autostop_store_owner_api_v1",
                "status": "blocked",
                "error": {"code": "store_owner_prepare_mode_invalid"},
                "summary": {"operation_id": operation_id},
                "data_included": False,
            }
        normalized_target = str(target_id or "").strip()
        contract_id: str | None = None
        effective_correlation_id = str(correlation_id or "").strip()
        if method != "GET":
            parameter_names = capability.get("path_parameters")
            names = parameter_names if isinstance(parameter_names, list) else []
            supplied_path_parameters = path_parameters if isinstance(path_parameters, dict) else {}
            expected_target = ""
            if len(names) == 1:
                expected_target = str(supplied_path_parameters.get(str(names[0])) or "").strip()
            elif len(names) > 1:
                expected_target = f"path:{capability.get('concrete_path') or ''!s}"
            elif capability.get("revision_required") is False:
                expected_target = f"collection:{capability.get('path') or ''!s}"
            else:
                expected_target = f"path:{capability.get('concrete_path') or ''!s}"
            if expected_target and normalized_target != expected_target:
                return {
                    "ok": False,
                    "format": "autostop_store_owner_api_v1",
                    "status": "blocked",
                    "error": {"code": "store_owner_target_binding_mismatch"},
                    "summary": {"expected_target_ref": expected_target},
                    "data_included": False,
                }
            if normalized_mode != "revision" and not effective_correlation_id:
                return {
                    "ok": False,
                    "format": "autostop_store_owner_api_v1",
                    "status": "blocked",
                    "error": {"code": "store_owner_correlation_id_required"},
                    "summary": {"operation_id": operation_id},
                    "data_included": False,
                }
            if normalized_mode != "revision":
                contract = prepare_action_contract(
                    domain="store_owner_api",
                    action="execute_owner_api",
                    target_id=target_id,
                    planned_changes={
                        "operation_id": operation_id,
                        "method": method,
                        "path_template": str(capability.get("path") or ""),
                        "risk": str(capability.get("risk") or ""),
                        "schema_hash": str(capability.get("schema_hash") or ""),
                        "concrete_path": str(capability.get("concrete_path") or ""),
                        "query_fields": capability.get("query_fields") or [],
                        "query_sha256": str(capability.get("query_sha256") or ""),
                        "request_sha256": str(capability.get("request_sha256") or ""),
                        "plan_hash": str(capability.get("plan_hash") or ""),
                        "verification_class": str(capability.get("verification_class") or ""),
                        "body_fields": sorted(body) if isinstance(body, dict) else [],
                        "form_fields": sorted(form) if isinstance(form, dict) else [],
                        "file_fields": sorted(
                            {
                                str(item.get("field") or "")
                                for item in files or []
                                if isinstance(item, dict) and str(item.get("field") or "")
                            }
                        ),
                    },
                    owner_intent=owner_intent,
                    expected_revision=expected_revision,
                    idempotency_key=idempotency_key,
                    correlation_id=correlation_id,
                    dry_run=(
                        normalized_prepare_for_mode == "dry_run"
                        if normalized_mode == "prepare"
                        else normalized_mode != "apply"
                    ),
                )
                if not contract.get("ok") or not (
                    isinstance(contract.get("execution"), dict) and contract["execution"].get("ready")
                ):
                    return {
                        "ok": False,
                        "format": "autostop_store_owner_api_v1",
                        "status": "blocked",
                        "error": {"code": "store_owner_action_contract_blocked"},
                        "summary": {
                            "contract_id": contract.get("contract_id"),
                            "blocking_reasons": (
                                contract.get("preflight", {}).get("blocking_reasons", [])
                                if isinstance(contract.get("preflight"), dict)
                                else []
                            ),
                        },
                        "data_included": False,
                    }
                contract_id = str(contract.get("contract_id") or "") or None
                effective_correlation_id = str(contract.get("correlation_id") or "")
                if normalized_mode in {"dry_run", "apply"}:
                    normalized_expected_contract = str(expected_contract_id or "").strip()
                    if not normalized_expected_contract:
                        return {
                            "ok": False,
                            "format": "autostop_store_owner_api_v1",
                            "status": "blocked",
                            "error": {"code": "store_owner_expected_contract_id_required"},
                            "summary": {"operation_id": operation_id},
                            "data_included": False,
                        }
                    if normalized_expected_contract != contract_id:
                        return {
                            "ok": False,
                            "format": "autostop_store_owner_api_v1",
                            "status": "conflict",
                            "error": {"code": "store_owner_action_contract_mismatch"},
                            "summary": {"operation_id": operation_id},
                            "data_included": False,
                        }
        technical_meta = {
            "contract_id": contract_id,
            "operation_id": operation_id,
            "request_sha256": str(capability.get("request_sha256") or ""),
            "schema_hash": str(capability.get("schema_hash") or ""),
            "verification_class": str(capability.get("verification_class") or ""),
            "correlation_id": effective_correlation_id or None,
            "target_ref_sha256": (
                hashlib.sha256(f"target:{normalized_target}".encode()).hexdigest() if normalized_target else None
            ),
            "expected_revision_sha256": (
                hashlib.sha256(f"expected:{expected_revision}".encode()).hexdigest()
                if expected_revision is not None
                else None
            ),
        }
        if normalized_mode == "prepare":
            return {
                "ok": True,
                "format": "autostop_store_owner_api_v1",
                "status": "validated",
                "summary": {
                    "operation_id": operation_id,
                    "method": method,
                    "risk": str(capability.get("risk") or ""),
                    "prepared_for_mode": normalized_prepare_for_mode,
                    "request_dispatched": False,
                },
                "meta": {
                    **technical_meta,
                    "request_dispatched": False,
                    "domain_handler_executed": False,
                },
                "data_included": False,
            }
        result = store_owner_client.invoke(
            operation_id=operation_id,
            mode=mode,
            path_parameters=path_parameters,
            query=query,
            body=body,
            form=form,
            files=files,
            owner_intent=owner_intent,
            idempotency_key=idempotency_key,
            correlation_id=effective_correlation_id,
            expected_revision=expected_revision,
            dry_run_proof=dry_run_proof,
            allow_binary_response=allow_binary_response,
            expected_plan_hash=str(capability.get("plan_hash") or "") or None,
        )
        meta = result.setdefault("meta", {})
        if isinstance(meta, dict):
            meta.update(technical_meta)
        return result


def register_store_read_tools(server: Any, store_adapter: StoreIntegration) -> None:
    @server.tool(
        name="store_search",
        description=(
            "INTERNAL_ONLY: Search an allowlisted Store entity with bounded pagination and redacted contacts."
        ),
    )
    def store_search_tool(
        entity: str,
        query: str = "",
        filters: dict[str, Any] | None = None,
        cursor: str | None = None,
        limit: int = 25,
    ) -> dict[str, Any]:
        return store_adapter.search(entity=entity, query=query, filters=filters, cursor=cursor, limit=limit)

    @server.tool(
        name="store_entity_context",
        description=(
            "INTERNAL_ONLY: Read one exact Store entity. General reads are redacted; full quote data uses "
            "the scoped credential and remains transient."
        ),
    )
    def store_entity_context_tool(
        entity: str,
        entity_id: str,
        detail: str = "summary",
    ) -> dict[str, Any]:
        return store_adapter.entity_context(entity=entity, entity_id=entity_id, detail=detail)
