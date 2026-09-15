from __future__ import annotations

import hashlib
from collections.abc import Collection
from typing import Any

from mcp.types import ToolAnnotations

from .action_contract import prepare_action_contract
from .catalog_adapters import build_oem_parts_provider_plan, catalog_provider_status
from .catalog_clients import (
    PARTSAPI_OPERATIONS,
    exist_price_lookup,
    partsapi_catalog_lookup,
    public_aftermarket_catalog_lookup,
)
from .config import (
    get_store_api_url,
    get_store_manage_token,
    get_store_owner_token,
    get_store_quote_token,
    get_store_read_token,
)
from .partsapi_category_index import (
    explain_partsapi_category_for_intent,
    search_partsapi_category_index,
    validate_partsapi_category_index,
)
from .public_automotive_evidence import lookup_public_automotive_evidence
from .source_catalog import recommend_automotive_sources
from .storage import StoreState
from .store_api import StoreApiClient
from .store_analytics import get_store_analytics_report
from .store_integration import StoreIntegration
from .store_owner_api import StoreOwnerApiClient
from .store_quote_conductor import StoreQuoteConductor, StoreQuoteOwnerApi
from .vehicle_identity import decode_vehicle_identities, decode_vehicle_identity
from .vin_parts_benchmark import benchmark_vin_parts_lookup
from .vin_oem_resolver import resolve_vin_oem_parts
from .vin_lookup import lookup_original_parts
from .work_pricing import estimate_repair_work_cost


# Registration is intentionally declarative; each nested tool delegates to tested domain functions or storage methods.
def register_manager_tools(  # noqa: C901
    server: Any,
    store: StoreState | None = None,
    store_client: StoreApiClient | None = None,
    include_tools: Collection[str] | None = None,
) -> None:
    original_tool = server.tool
    if include_tools is not None:
        selected_tools = frozenset(include_tools)

        def filtered_tool(*args: Any, **kwargs: Any) -> Any:
            if str(kwargs.get("name") or "") in selected_tools:
                return original_tool(*args, **kwargs)
            return lambda function: function

        server.tool = filtered_tool
    memory = store or StoreState()
    store_adapter = StoreIntegration(
        client=store_client
        or StoreApiClient(
            api_url=get_store_api_url(),
            read_token=get_store_read_token(),
            manage_token=get_store_manage_token(),
            quote_token=get_store_quote_token(),
        ),
        store=memory,
    )
    store_owner_client = StoreOwnerApiClient(
        agent_api_url=get_store_api_url(),
        owner_token=get_store_owner_token(),
    )
    quote_conductor = StoreQuoteConductor(
        store=memory,
        gateway=StoreQuoteOwnerApi(store_owner_client),
    )

    @server.tool(
        name="get_store_analytics_report",
        description=(
            "READ_ONLY RAW_CAPABILITY: Return one aggregate Store report for the requested period; "
            "never return raw events, identifiers, search text or customer data."
        ),
    )
    def get_store_analytics_report_tool(
        query: str = "",
        period: str = "auto",
        date_from: str | None = None,
        date_to: str | None = None,
        top_limit: int = 10,
    ) -> dict[str, Any]:
        return get_store_analytics_report(
            api_url=get_store_api_url(),
            read_token=get_store_read_token(),
            query=query,
            period=period,
            date_from=date_from,
            date_to=date_to,
            top_limit=top_limit,
        )

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
        prepared = store_owner_client.prepare_invocation(
            operation_id=operation_id,
            path_parameters=path_parameters,
            query=query,
            body=body,
            form=form,
            files=files,
            expected_revision=expected_revision,
        )
        if not prepared.get("ok"):
            return prepared
        raw_capability = prepared.get("summary")
        capability: dict[str, Any] = raw_capability if isinstance(raw_capability, dict) else {}
        method = str(capability.get("method") or "").upper()
        normalized_mode = str(mode or "").strip().casefold()
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

    @server.tool(
        name="store_runtime_status",
        description=("INTERNAL_ONLY: Return redacted Store adapter readiness and optional live health for Gateway v2."),
    )
    def store_runtime_status_tool(
        live: bool = False,
        bootstrap_snapshot: bool = False,
    ) -> dict[str, Any]:
        return store_adapter.runtime_status(
            live=live,
            bootstrap_snapshot=bootstrap_snapshot,
        )

    @server.tool(
        name="store_digest",
        description=(
            "INTERNAL_ONLY: Read one bounded Store digest page. Acknowledge its cursor before advancing; "
            "the first read creates a baseline and Manager persists no raw payload."
        ),
    )
    def store_digest_tool(
        baseline: bool = False,
        since: str | None = None,
        cursor: str | None = None,
        ack_token: str | None = None,
        limit: int = 25,
        stream: str = "store_digest",
    ) -> dict[str, Any]:
        return store_adapter.digest(
            baseline=baseline,
            since=since,
            cursor=cursor,
            ack_token=ack_token,
            limit=limit,
            stream=stream,
        )

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

    @server.tool(
        name="download_store_quote_vin_photo",
        description=("INTERNAL_ONLY: Read a bounded transient JPEG preview for one exact Store quote VIN photo."),
        annotations=ToolAnnotations(
            title="Store Quote VIN Photo Preview",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    def download_store_quote_vin_photo_tool(
        quote_request_id: str,
        expected_photo_sha256: str,
    ) -> dict[str, Any]:
        return store_adapter.quote_vin_photo_preview(
            quote_request_id=quote_request_id,
            expected_photo_sha256=expected_photo_sha256,
        )

    @server.tool(
        name="store_management_action",
        description=(
            "INTERNAL_ONLY: Run one allowlisted Store management operation with ActionContractV2, exact "
            "preread, dry-run/apply, idempotency, optimistic concurrency and reread."
        ),
    )
    def store_management_action_tool(
        domain: str,
        action: str,
        target_id: str,
        planned_changes: dict[str, Any],
        owner_intent: str,
        expected_updated_at: str,
        idempotency_key: str,
        correlation_id: str,
        mode: str = "dry_run",
    ) -> dict[str, Any]:
        return store_adapter.management_action(
            domain=domain,
            action=action,
            target_id=target_id,
            planned_changes=planned_changes,
            owner_intent=owner_intent,
            expected_updated_at=expected_updated_at,
            idempotency_key=idempotency_key,
            correlation_id=correlation_id,
            mode=mode,
        )

    @server.tool(
        name="store_quote_conductor",
        description=(
            "INTERNAL_ONLY: Advance one Store quote through Admin V2; use the work Telegram workflow for dialogue. "
            "Supports start, status, evidence, draft, publish, reopen, order, handoff and decline; writes use the exact "
            "current quote and a confirmed reread."
        ),
    )
    def store_quote_conductor_tool(
        operation: str,
        quote_request_id: str = "",
        run_id: int | None = None,
        expected_state_version: int | None = None,
        expected_revision: str = "",
        idempotency_key: str = "",
        correlation_id: str = "",
        entries: list[dict[str, Any]] | None = None,
        coverage: list[dict[str, Any]] | None = None,
        customer_response: str = "",
        evidence: dict[str, Any] | None = None,
        consent_context_hash: str = "",
        published_snapshot_hash: str = "",
        mode: str = "apply",
    ) -> dict[str, Any]:
        return quote_conductor.execute(
            operation=operation,
            quote_request_id=quote_request_id,
            run_id=run_id,
            expected_state_version=expected_state_version,
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            correlation_id=correlation_id,
            entries=entries,
            coverage=coverage,
            customer_response=customer_response,
            evidence=evidence,
            consent_context_hash=consent_context_hash,
            published_snapshot_hash=published_snapshot_hash,
            mode=mode,
        )

    server.tool(
        name="prepare_action_contract",
        description=(
            "Build a connector-neutral ActionContractV2 for CRM, AutoStop App store, finance, inventory, documents, files, or Gmail writes. "
            "Requires task intent, exact target where applicable, idempotency, concurrency, automatic preflight, compensation, "
            "and readback verification; never performs the write."
        ),
    )(prepare_action_contract)

    @server.tool(
        name="lookup_original_parts",
        description=(
            "Build a VIN, chassis, or market-code OEM lookup dossier with catalog routes, OEM candidates, confidence, and missing context."
        ),
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False),
    )
    def lookup_original_parts_tool(
        identifier: str,
        model_year: int | None = None,
        make_hint: str | None = None,
        part_name: str | None = None,
        part_group: str | None = None,
        side: str | None = None,
        position: str | None = None,
        old_part_number: str | None = None,
        captured_oem_number: str | None = None,
        captured_source: str | None = None,
        captured_supersedes: str | None = None,
        captured_note: str | None = None,
    ) -> dict[str, Any]:
        return lookup_original_parts(
            identifier,
            model_year=model_year,
            make_hint=make_hint,
            part_name=part_name,
            part_group=part_group,
            side=side,
            position=position,
            old_part_number=old_part_number,
            captured_oem_number=captured_oem_number,
            captured_source=captured_source,
            captured_supersedes=captured_supersedes,
            captured_note=captured_note,
        )

    @server.tool(
        name="estimate_repair_work_cost",
        description=(
            "Build a read-only multi-source labor estimate from aggregate-only closed repair-order experience, "
            "public Russia STO labor-only prices, exact vehicle context, and norm-hours/labor-time plausibility. "
            "Returns evidence families, a reconciled recommendation, confidence, gaps, and next actions without writes."
        ),
    )
    def estimate_repair_work_cost_tool(
        vehicle: str | None = None,
        vin: str | None = None,
        chassis: str | None = None,
        make: str | None = None,
        model: str | None = None,
        year: int | str | None = None,
        engine: str | None = None,
        transmission: str | None = None,
        work_items: str | list[str] | None = None,
        complaint: str | None = None,
        city: str = "Красноярск",
        quotes_json: list[dict[str, Any]] | dict[str, Any] | None = None,
        auto_research: bool = True,
        labor_time_policy: str = "public_only",
        use_internal_experience: bool = True,
    ) -> dict[str, Any]:
        return estimate_repair_work_cost(
            vehicle=vehicle,
            vin=vin,
            chassis=chassis,
            make=make,
            model=model,
            year=year,
            engine=engine,
            transmission=transmission,
            work_items=work_items,
            complaint=complaint,
            city=city,
            quotes_json=quotes_json,
            auto_research=auto_research,
            labor_time_policy=labor_time_policy,
            use_internal_experience=use_internal_experience,
        )

    @server.tool(
        name="decode_vehicle_identity",
        description=(
            "Build a source-aware vehicle identity dossier from a VIN/frame/body number: "
            "classification, check digit/model-year diagnostics, vPIC/WMI/platform evidence, "
            "CRM-context conflicts, confidence, and required EPC/API sources for parts lookup."
        ),
        annotations=ToolAnnotations(
            title="Vehicle Identity",
            readOnlyHint=True,
            destructiveHint=False,
        ),
    )
    def decode_vehicle_identity_tool(
        identifier: str,
        vehicle: str | None = None,
        make: str | None = None,
        model: str | None = None,
        model_year: int | None = None,
        engine: str | None = None,
        transmission: str | None = None,
        drivetrain: str | None = None,
        market: str | None = None,
        source_confidence: float | None = None,
        live_vpic: bool = True,
        live_wmi: bool = True,
    ) -> dict[str, Any]:
        return decode_vehicle_identity(
            identifier,
            crm_context={
                "vehicle": vehicle,
                "make": make,
                "model": model,
                "model_year": model_year,
                "engine": engine,
                "transmission": transmission,
                "drivetrain": drivetrain,
                "market": market,
                "source_confidence": source_confidence,
            },
            model_year=model_year,
            make_hint=make,
            live_vpic=live_vpic,
            live_wmi=live_wmi,
        )

    server.tool(
        name="decode_vehicle_identities",
        description=(
            "Batch vehicle identity dossiers for VIN/frame/body-number lists. "
            "Returns per-identifier confidence, conflicts, adapter status, and required next EPC/API sources."
        ),
    )(decode_vehicle_identities)

    server.tool(
        name="catalog_provider_status",
        description=(
            "Report configured VIN/OEM/cross/procurement provider readiness without exposing secret values. "
            "Use before claiming live catalog or supplier API access."
        ),
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False),
    )(catalog_provider_status)

    server.tool(
        name="plan_oem_parts_providers",
        description=(
            "Build provider readiness and blocker plan for VIN/frame or a vehicle-parameter profile -> OEM candidates "
            "-> crosses/applicability -> procurement/RF market price. An empty identifier is allowed when the vehicle "
            "profile is supplied. Does not call suppliers or write CRM."
        ),
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False),
    )(build_oem_parts_provider_plan)

    @server.tool(
        name="partsapi_catalog_lookup",
        description=(
            "Read-only PartsAPI lookup; dry_run sends no request. Valid operation values: "
            + ", ".join(PARTSAPI_OPERATIONS)
            + ". Check catalog_provider_status.operation_status for each operation's credentials and required parameters; "
            "configured access does not prove provider success. For parts_by_vin, part_type defaults to oem; "
            "use omit/non-oem to skip the type parameter."
        ),
        annotations=ToolAnnotations(
            title="PartsAPI Catalog Lookup",
            readOnlyHint=True,
            destructiveHint=False,
        ),
    )
    def partsapi_catalog_lookup_tool(
        operation: str,
        identifier: str | None = None,
        registration_number: str | None = None,
        part_number: str | None = None,
        article_id: str | int | None = None,
        brand: str | None = None,
        part_type: str | None = None,
        category: str | None = None,
        vehicle_type: str | None = None,
        type_id: str | None = None,
        lang: str | None = None,
        lang_id: int | None = None,
        make_name_seo: str | None = None,
        model_id: str | int | None = None,
        motor_id: str | int | None = None,
        top_category_id: str | int | None = None,
        sub_category_id: str | int | None = None,
        car_id: str | int | None = None,
        timeout: float = 20.0,
        max_attempts: int = 1,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        return partsapi_catalog_lookup(
            operation=operation,
            identifier=identifier,
            registration_number=registration_number,
            part_number=part_number,
            article_id=article_id,
            brand=brand,
            part_type=part_type,
            category=category,
            vehicle_type=vehicle_type,
            type_id=type_id,
            lang=lang,
            lang_id=lang_id,
            make_name_seo=make_name_seo,
            model_id=model_id,
            motor_id=motor_id,
            top_category_id=top_category_id,
            sub_category_id=sub_category_id,
            car_id=car_id,
            timeout=timeout,
            max_attempts=max_attempts,
            dry_run=dry_run,
        )

    server.tool(
        name="search_partsapi_category_index",
        description="Search the local PartsAPI numeric category index by query/intent without live calls or secrets.",
    )(search_partsapi_category_index)

    server.tool(
        name="explain_partsapi_category_for_intent",
        description="Explain why a PartsAPI numeric category was selected for a part intent.",
    )(explain_partsapi_category_for_intent)

    server.tool(
        name="validate_partsapi_category_index",
        description="Validate the tracked local PartsAPI category index fixture without exposing secrets or identifiers.",
    )(validate_partsapi_category_index)

    @server.tool(
        name="public_aftermarket_catalog_lookup",
        description=(
            "Call public aftermarket catalogs by part/OE number. Supports MANN-FILTER and DENSO. "
            "Catalog data enriches a search "
            "but is not VIN-specific OEM EPC proof, fitment proof, or procurement pricing."
        ),
    )
    def public_aftermarket_catalog_lookup_tool(
        provider: str,
        part_number: str,
        page_size: int = 5,
        country: str = "europe",
        include_detail: bool = True,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        return public_aftermarket_catalog_lookup(
            provider=provider,
            part_number=part_number,
            page_size=page_size,
            country=country,
            include_detail=include_detail,
            dry_run=dry_run,
        )

    @server.tool(
        name="exist_price_lookup",
        description=(
            "Call or dry-run public read-only Exist article lookup for catalog disambiguation, analog visibility, "
            "retail price benchmark, and lead time. Uses office 905 by default; returns public_retail_reference only."
        ),
    )
    def exist_price_lookup_tool(
        part_number: str,
        brand: str | None = None,
        pid: str | None = None,
        office_id: int = 905,
        max_candidates: int = 5,
        max_offers: int = 10,
        include_more_offers: bool = False,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        return exist_price_lookup(
            part_number=part_number,
            brand=brand,
            pid=pid,
            office_id=office_id,
            max_candidates=max_candidates,
            max_offers=max_offers,
            include_more_offers=include_more_offers,
            dry_run=dry_run,
        )

    server.tool(
        name="resolve_vin_oem_parts",
        description=(
            "Resolve one VIN/frame/body-number and requested part into a read-only VinOemResolution: "
            "identity, part intent, PartsAPI category, OEM candidates, enrichment, readiness gates, manual actions, and CRM gate."
        ),
        annotations=ToolAnnotations(
            title="OEM Catalog Candidates",
            readOnlyHint=True,
            destructiveHint=False,
        ),
    )(resolve_vin_oem_parts)

    server.tool(
        name="benchmark_vin_parts_lookup",
        description=(
            "Read-only benchmark for a batch of CRM VIN/frame/body-number items: identity confidence, part-intent recognition, "
            "safe public search templates, provider blockers, and PartsAPI dry-run readiness. Raw identifiers are redacted from output."
        ),
    )(benchmark_vin_parts_lookup)

    server.tool(
        name="recommend_automotive_sources",
        description=(
            "Recommend authoritative repair, TSB, recall, diagnostic, wiring, labor, fluid, torque, or OEM source routes "
            "by brand and data type without copying licensed source content."
        ),
    )(recommend_automotive_sources)

    server.tool(
        name="lookup_public_automotive_evidence",
        description=(
            "Read compact official public automotive evidence: NHTSA model-level recalls, optional manufacturer-"
            "communications/TSB metadata, and applicable Mercedes/ZF fluid-reference routes. Does not use a VIN "
            "for campaign status, copy manuals, write CRM, or replace OEM service documentation."
        ),
        annotations=ToolAnnotations(
            title="Public Automotive Evidence",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
    )(lookup_public_automotive_evidence)

    if include_tools is not None:
        server.tool = original_tool


def register_manager_memory_tools(
    server: Any,
    store: StoreState | None = None,
    store_client: StoreApiClient | None = None,
    include_tools: Collection[str] | None = None,
) -> None:
    """Keep CRM's Python import contract, not its former Manager MCP surface."""
    from .crm_compat import register_crm_workflow_tools

    state = store or StoreState()
    register_manager_tools(server, store=state, store_client=store_client, include_tools=include_tools)
    if include_tools is not None:
        register_crm_workflow_tools(server, state, include_tools)
