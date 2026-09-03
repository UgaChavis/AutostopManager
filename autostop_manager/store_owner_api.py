from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError
from referencing.exceptions import Unresolvable

from .config import normalize_store_api_url


STORE_OWNER_FORMAT = "autostop_store_owner_api_v1"
MAX_OPENAPI_BYTES = 3 * 1024 * 1024
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_BINARY_RESPONSE_BYTES = 10 * 1024 * 1024
MAX_REQUEST_BYTES = 2 * 1024 * 1024
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_FILES_BYTES = 20 * 1024 * 1024
MAX_QUERY_BYTES = 16 * 1024
MAX_CAPABILITIES = 250
MAX_CONTAINER_ITEMS = 1000
MAX_JSON_DEPTH = 16
MAX_INPUT_CONTRACT_BYTES = 256 * 1024
MAX_INPUT_SCHEMA_DEPTH = 16
MAX_INPUT_SCHEMA_NODES = 4096
MAX_INPUT_SCHEMA_DEFINITIONS = 256
MAX_INPUT_SCHEMA_ITEMS = 1000
MAX_INPUT_SCHEMA_STRING_LENGTH = 4096
OPENAPI_CACHE_SECONDS = 60
OWNER_INTENT_HEADER = "X-Autostop-Owner-Intent"
CORRELATION_HEADER = "X-Correlation-ID"
EXPECTED_REVISION_HEADER = "X-Autostop-Expected-Revision"
DRY_RUN_PROOF_HEADER = "X-Autostop-Dry-Run-Proof"
ACTION_MODE_HEADER = "X-Autostop-Action-Mode"
IDEMPOTENCY_HEADER = "Idempotency-Key"
OWNER_REVISION_PATH = "/internal/owner/v1/write-revision"
OWNER_PREFLIGHT_CONTRACT_VERSION = "store-owner-preflight-v2"
OWNER_PREFLIGHT_MAX_TTL = timedelta(minutes=5, seconds=30)
_OPERATION_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,199}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,159}$")
_OPAQUE_RECEIPT = re.compile(r"^[0-9a-f]{64}$")
_MEDIA_TYPE = re.compile(r"^[a-z0-9][a-z0-9!#$&^_.+-]{0,63}/[a-z0-9][a-z0-9!#$&^_.+-]{0,127}$")
_PATH_PARAMETER = re.compile(r"\{([^{}]+)\}")
_HTTP_METHODS = frozenset({"get", "post", "put", "patch", "delete"})
_JSON_SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"
_VERIFICATION_CLASSES = frozenset(
    {
        "absence_plus_audit",
        "collection_membership",
        "exact_entity",
        "operation_specific_state",
    }
)
_SESSION_BOUNDARIES = frozenset(
    {
        ("POST", "/api/v1/admin/auth/login"),
        ("POST", "/api/v1/admin/auth/logout"),
    }
)
SAFE_REVERSIBLE_COLLECTION_CREATE_PATHS = frozenset(
    {
        "/api/v1/categories",
        "/api/v1/customers",
        "/api/v1/manufacturers",
        "/api/v1/parts",
    }
)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@dataclass(frozen=True)
class OwnerCapability:
    operation_id: str
    method: str
    path_template: str
    risk: str
    request_content_types: tuple[str, ...]
    request_required: bool
    path_parameters: tuple[str, ...]
    schema_hash: str = ""
    query_parameters: tuple[str, ...] = ()
    required_query_parameters: tuple[str, ...] = ()
    request_schemas: tuple[tuple[str, Any], ...] = field(default=(), repr=False, compare=False)
    response_content_types: tuple[str, ...] = ()
    response_statuses: tuple[str, ...] = ()
    response_content_contracts: tuple[tuple[str, str], ...] = field(default=(), repr=False, compare=False)
    response_schemas: tuple[tuple[str, str, Any], ...] = field(default=(), repr=False, compare=False)
    response_contract_enforced: bool = field(default=False, repr=False, compare=False)
    path_parameter_schemas: tuple[tuple[str, Any], ...] = field(default=(), repr=False, compare=False)
    query_parameter_schemas: tuple[tuple[str, Any], ...] = field(default=(), repr=False, compare=False)
    schema_components: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    def compact(self) -> dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "method": self.method,
            "path": self.path_template,
            "risk": self.risk,
            "request_content_types": list(self.request_content_types),
            "request_required": self.request_required,
            "path_parameters": list(self.path_parameters),
            "schema_hash": self.schema_hash,
            "query_parameters": list(self.query_parameters),
            "required_query_parameters": list(self.required_query_parameters),
            "response_content_types": list(self.response_content_types),
            "response_statuses": list(self.response_statuses),
        }


@dataclass(frozen=True)
class _PreparedInvocation:
    capability: OwnerCapability
    concrete_path: str
    query: dict[str, Any]
    payload: dict[str, Any]
    expected_revision: str | None
    plan_hash: str
    verification_class: str

    def compact(self) -> dict[str, Any]:
        return {
            **self.capability.compact(),
            "concrete_path": self.concrete_path,
            "query_fields": sorted(self.query),
            "query_sha256": _canonical_hash(self.query),
            "request_sha256": self.payload["canonical_sha256"],
            "planned_body_fields": self.payload["field_names"],
            "planned_files": self.payload["file_metadata"],
            "plan_hash": self.plan_hash,
            "verification_class": self.verification_class,
            "revision_required": _expected_revision_required(self.capability),
            "schema_validation": "passed",
        }


@dataclass(frozen=True)
class _OwnerRevision:
    current_revision: str | None
    revision_kind: str
    route_key: str
    expected_revision_required: bool
    contract_version: str


class StoreOwnerApiClient:
    """Bounded owner-principal transport over the Store's existing employee API."""

    def __init__(
        self,
        *,
        agent_api_url: str,
        owner_token: str,
        timeout: float = 15.0,
        openapi_cache_seconds: int = OPENAPI_CACHE_SECONDS,
    ) -> None:
        self.origin = _store_origin(agent_api_url)
        self.owner_token = str(owner_token or "").strip()
        self.timeout = max(1.0, min(float(timeout), 60.0))
        self.openapi_cache_seconds = max(1, min(int(openapi_cache_seconds), 600))
        self._cache_lock = threading.Lock()
        self._cached_at = 0.0
        self._capabilities: dict[str, OwnerCapability] = {}
        self._loaded_from_openapi = False

    def list_capabilities(
        self,
        *,
        query: str = "",
        limit: int = 200,
        operation_id: str = "",
    ) -> dict[str, Any]:
        normalized_operation_id = str(operation_id or "").strip()
        if normalized_operation_id:
            return self.describe_operation(normalized_operation_id)
        capabilities = self._load_capabilities()
        if isinstance(capabilities, dict) and capabilities.get("ok") is False:
            return capabilities
        normalized_query = str(query or "").strip().casefold()[:200]
        bounded_limit = max(1, min(int(limit), MAX_CAPABILITIES))
        values = list(capabilities.values())
        if normalized_query:
            values = [
                item
                for item in values
                if normalized_query in f"{item.operation_id} {item.method} {item.path_template} {item.risk}".casefold()
            ]
        values.sort(key=lambda item: (item.path_template, item.method, item.operation_id))
        return {
            "ok": True,
            "format": STORE_OWNER_FORMAT,
            "status": "completed",
            "summary": {
                "matches": len(values),
                "returned": min(len(values), bounded_limit),
                "owner_api_ready": True,
            },
            "items": [item.compact() for item in values[:bounded_limit]],
            "data_included": False,
        }

    def describe_operation(self, operation_id: str) -> dict[str, Any]:
        normalized = str(operation_id or "").strip()
        if _OPERATION_ID.fullmatch(normalized) is None:
            return _error("store_owner_operation_invalid")
        capabilities = self._load_capabilities()
        if isinstance(capabilities, dict) and capabilities.get("ok") is False:
            return capabilities
        capability = capabilities.get(normalized)
        if capability is None:
            return _error("store_owner_operation_not_found")
        try:
            input_contract = _operation_input_contract(capability)
        except (TypeError, ValueError, OverflowError) as exc:
            error_code = str(exc)
            if not error_code.startswith("store_owner_input_contract_"):
                error_code = "store_owner_input_contract_invalid"
            return _error(error_code)
        return {
            "ok": True,
            "format": STORE_OWNER_FORMAT,
            "status": "completed",
            "summary": {
                **capability.compact(),
                "owner_api_ready": True,
                "input_contract_bytes": len(_canonical_json_bytes(input_contract)),
            },
            "input_contract": input_contract,
            "data_included": False,
        }

    def prepare_invocation(
        self,
        *,
        operation_id: str,
        path_parameters: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
        body: Any = None,
        form: dict[str, Any] | None = None,
        files: list[dict[str, Any]] | None = None,
        expected_revision: str | None = None,
    ) -> dict[str, Any]:
        """Validate one exact invocation without dispatching or returning raw inputs."""

        prepared = self._prepare_request(
            operation_id=operation_id,
            path_parameters=path_parameters,
            query=query,
            body=body,
            form=form,
            files=files,
            expected_revision=expected_revision,
            force_refresh=False,
            enforce_revision=False,
        )
        if isinstance(prepared, dict):
            return prepared
        return {
            "ok": True,
            "format": STORE_OWNER_FORMAT,
            "status": "validated",
            "summary": prepared.compact(),
            "data_included": False,
        }

    def invoke(
        self,
        *,
        operation_id: str,
        mode: str,
        path_parameters: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
        body: Any = None,
        form: dict[str, Any] | None = None,
        files: list[dict[str, Any]] | None = None,
        owner_intent: str = "",
        idempotency_key: str = "",
        correlation_id: str = "",
        expected_revision: str | None = None,
        dry_run_proof: str | None = None,
        allow_binary_response: bool = False,
        expected_plan_hash: str | None = None,
    ) -> dict[str, Any]:
        normalized_mode = str(mode or "").strip().casefold()
        prepared = self._prepare_request(
            operation_id=operation_id,
            path_parameters=path_parameters,
            query=query,
            body=body,
            form=form,
            files=files,
            expected_revision=expected_revision,
            force_refresh=normalized_mode == "apply",
            enforce_revision=normalized_mode != "revision",
        )
        if isinstance(prepared, dict):
            return prepared
        normalized_expected_plan_hash = str(expected_plan_hash or "").strip()
        if normalized_expected_plan_hash and not hmac.compare_digest(
            normalized_expected_plan_hash,
            prepared.plan_hash,
        ):
            return _error("store_owner_plan_changed", status="conflict")
        capability = prepared.capability
        is_read = capability.method == "GET"
        if is_read and normalized_mode != "read":
            return _error("store_owner_read_mode_required")
        if not is_read and normalized_mode not in {"revision", "dry_run", "apply"}:
            return _error("store_owner_write_mode_invalid")
        if allow_binary_response and not _binary_response_allowed(capability):
            return _error("store_owner_binary_response_not_allowed")
        if not is_read and normalized_mode != "revision":
            metadata_error = _validate_write_metadata(
                owner_intent=owner_intent,
                idempotency_key=idempotency_key,
                correlation_id=correlation_id,
            )
            if metadata_error:
                return _error(metadata_error)
            if normalized_mode == "apply" and _OPAQUE_RECEIPT.fullmatch(str(dry_run_proof or "").strip()) is None:
                return _error("store_owner_dry_run_proof_required")

        if not is_read and normalized_mode == "revision":
            current_revision = self._read_write_revision(
                capability=capability,
                concrete_path=prepared.concrete_path,
            )
            if isinstance(current_revision, dict):
                return current_revision
            if (
                current_revision.expected_revision_required != _expected_revision_required(capability)
                or current_revision.contract_version != OWNER_PREFLIGHT_CONTRACT_VERSION
            ):
                return _error("store_owner_revision_contract_mismatch", status="blocked")
            return {
                "ok": True,
                "format": STORE_OWNER_FORMAT,
                "status": "completed",
                "summary": {
                    **prepared.compact(),
                    "current_revision": current_revision.current_revision,
                    "revision_kind": current_revision.revision_kind,
                    "route_key": current_revision.route_key,
                    "expected_revision_required": current_revision.expected_revision_required,
                    "contract_version": current_revision.contract_version,
                },
                "meta": {
                    "request_dispatched": True,
                    "domain_handler_executed": False,
                },
                "data_included": False,
            }

        if not is_read and normalized_mode == "dry_run" and _expected_revision_required(capability):
            current_revision = self._read_write_revision(
                capability=capability,
                concrete_path=prepared.concrete_path,
            )
            if isinstance(current_revision, dict):
                return current_revision
            if (
                not current_revision.expected_revision_required
                or current_revision.contract_version != OWNER_PREFLIGHT_CONTRACT_VERSION
            ):
                return _error("store_owner_revision_contract_mismatch", status="blocked")
            if current_revision.current_revision != prepared.expected_revision:
                return _error(
                    "store_owner_expected_revision_stale",
                    status="conflict",
                    current_revision=current_revision.current_revision,
                    revision_kind=current_revision.revision_kind,
                    route_key=current_revision.route_key,
                )

        return self._dispatch(
            capability=capability,
            concrete_path=prepared.concrete_path,
            query=prepared.query,
            payload=prepared.payload,
            owner_intent=owner_intent,
            idempotency_key=idempotency_key,
            correlation_id=correlation_id,
            expected_revision=prepared.expected_revision,
            dry_run_proof=str(dry_run_proof or "").strip() or None,
            action_mode=normalized_mode,
            planned_summary=prepared.compact(),
            allow_binary_response=allow_binary_response,
            verification_class=prepared.verification_class,
        )

    def _prepare_request(
        self,
        *,
        operation_id: str,
        path_parameters: dict[str, Any] | None,
        query: dict[str, Any] | None,
        body: Any,
        form: dict[str, Any] | None,
        files: list[dict[str, Any]] | None,
        expected_revision: str | None,
        force_refresh: bool,
        enforce_revision: bool,
    ) -> _PreparedInvocation | dict[str, Any]:
        normalized_operation = str(operation_id or "").strip()
        if _OPERATION_ID.fullmatch(normalized_operation) is None:
            return _error("store_owner_operation_invalid")
        capabilities = self._load_capabilities(force=force_refresh and self._loaded_from_openapi)
        if isinstance(capabilities, dict) and capabilities.get("ok") is False:
            return capabilities
        capability = capabilities.get(normalized_operation)
        if capability is None:
            return _error("store_owner_operation_not_found")
        try:
            revision = _validated_expected_revision(
                capability,
                expected_revision,
                enforce_required=enforce_revision,
            )
            concrete_path = _concrete_path(capability, path_parameters or {})
            normalized_query = _validated_query(query, capability=capability)
            normalized_form = _validated_mapping(form, name="form")
            _validate_json_value(body)
            request_payload = _request_payload(
                capability=capability,
                body=body,
                form=normalized_form,
                files=files or [],
            )
        except (SchemaError, ValueError) as exc:
            code = str(exc)
            if not code.startswith("store_owner_"):
                code = "store_owner_openapi_schema_invalid"
            return _error(code)
        plan_hash = _plan_hash(
            capability=capability,
            concrete_path=concrete_path,
            query=normalized_query,
            payload=request_payload,
            expected_revision=revision,
        )
        return _PreparedInvocation(
            capability=capability,
            concrete_path=concrete_path,
            query=normalized_query,
            payload=request_payload,
            expected_revision=revision,
            plan_hash=plan_hash,
            verification_class=_verification_class(capability),
        )

    def _load_capabilities(self, *, force: bool = False) -> dict[str, OwnerCapability] | dict[str, Any]:
        if not self.origin:
            return _error("store_owner_api_url_missing")
        if not self.owner_token:
            return _error("store_owner_token_missing")
        now = time.monotonic()
        with self._cache_lock:
            if not force and self._capabilities and now - self._cached_at < self.openapi_cache_seconds:
                return dict(self._capabilities)
        try:
            payload = self._read_openapi()
            capabilities = _parse_capabilities(payload)
        except (OSError, TypeError, UnicodeError, ValueError, URLError):
            return _error("store_owner_openapi_unavailable")
        with self._cache_lock:
            self._capabilities = capabilities
            self._cached_at = now
            self._loaded_from_openapi = True
        return dict(capabilities)

    def _read_openapi(self) -> dict[str, Any]:
        request = Request(
            f"{self.origin}/openapi.json",
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self.owner_token}",
                "User-Agent": "AutostopManager/0.1",
            },
            method="GET",
        )
        with build_opener(_NoRedirect()).open(request, timeout=self.timeout) as response:
            raw = response.read(MAX_OPENAPI_BYTES + 1)
        if len(raw) > MAX_OPENAPI_BYTES:
            raise ValueError("openapi_response_too_large")
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("openapi_response_invalid")
        return payload

    def _read_write_revision(
        self,
        *,
        capability: OwnerCapability,
        concrete_path: str,
    ) -> _OwnerRevision | dict[str, Any]:
        query = urlencode({"method": capability.method, "path": concrete_path})
        request = Request(
            f"{self.origin}{OWNER_REVISION_PATH}?{query}",
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self.owner_token}",
                "User-Agent": "AutostopManager/0.1",
            },
            method="GET",
        )
        try:
            with build_opener(_NoRedirect()).open(request, timeout=self.timeout) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                status_code = int(getattr(response, "status", 200))
        except HTTPError as exc:
            raw = exc.read(MAX_RESPONSE_BYTES + 1)
            return _http_error(exc.code, raw, is_write=False)
        except (TimeoutError, URLError, OSError):
            return _error("store_owner_revision_read_failed", request_dispatched=True)
        if len(raw) > MAX_RESPONSE_BYTES:
            return _error("store_owner_revision_response_too_large", request_dispatched=True)
        if not 200 <= status_code < 300:
            return _http_error(status_code, raw, is_write=False)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeError, ValueError, TypeError):
            return _error("store_owner_revision_response_invalid", request_dispatched=True)
        if not isinstance(payload, dict):
            return _error("store_owner_revision_response_invalid", request_dispatched=True)
        current_revision = payload.get("currentRevision")
        revision_kind = payload.get("revisionKind")
        route_key = payload.get("routeKey")
        expected_required = payload.get("expectedRevisionRequired")
        contract_version = payload.get("contractVersion")
        valid_revision = current_revision is None or (
            isinstance(current_revision, str)
            and 1 <= len(current_revision) <= 200
            and current_revision.isascii()
            and all(0x20 <= ord(character) <= 0x7E for character in current_revision)
        )
        if (
            payload.get("method") != capability.method
            or payload.get("path") != concrete_path
            or not valid_revision
            or revision_kind not in {"entity_updated_at", "revision_exempt", "route_opaque"}
            or not isinstance(route_key, str)
            or not 1 <= len(route_key) <= 700
            or not isinstance(expected_required, bool)
            or not isinstance(contract_version, str)
        ):
            return _error("store_owner_revision_response_invalid", request_dispatched=True)
        if expected_required and current_revision is None:
            return _error("store_owner_revision_response_invalid", request_dispatched=True)
        return _OwnerRevision(
            current_revision=current_revision,
            revision_kind=revision_kind,
            route_key=route_key,
            expected_revision_required=expected_required,
            contract_version=contract_version,
        )

    def _dispatch(
        self,
        *,
        capability: OwnerCapability,
        concrete_path: str,
        query: dict[str, Any],
        payload: dict[str, Any],
        owner_intent: str,
        idempotency_key: str,
        correlation_id: str,
        expected_revision: str | None,
        dry_run_proof: str | None,
        action_mode: str,
        planned_summary: dict[str, Any],
        allow_binary_response: bool,
        verification_class: str,
    ) -> dict[str, Any]:
        url = f"{self.origin}{concrete_path}"
        if query:
            url = f"{url}?{urlencode(query, doseq=True)}"
        headers = {
            "Accept": "application/json, application/octet-stream;q=0.5",
            "Authorization": f"Bearer {self.owner_token}",
            "User-Agent": "AutostopManager/0.1",
        }
        if capability.method != "GET":
            headers.update(
                {
                    IDEMPOTENCY_HEADER: str(idempotency_key).strip(),
                    CORRELATION_HEADER: str(correlation_id).strip(),
                    ACTION_MODE_HEADER: action_mode,
                }
            )
            if action_mode == "apply":
                headers[DRY_RUN_PROOF_HEADER] = str(dry_run_proof or "")
            if str(expected_revision or "").strip():
                headers[EXPECTED_REVISION_HEADER] = str(expected_revision).strip()
            headers[OWNER_INTENT_HEADER] = _owner_intent_header_value(owner_intent)
        if payload["content_type"]:
            headers["Content-Type"] = payload["content_type"]
        request = Request(
            url,
            data=payload["data"],
            headers=headers,
            method=capability.method,
        )
        try:
            with build_opener(_NoRedirect()).open(request, timeout=self.timeout) as response:
                content_type = str(response.headers.get("Content-Type") or "").split(";", 1)[0].casefold()
                response_limit = (
                    MAX_BINARY_RESPONSE_BYTES
                    if allow_binary_response
                    and content_type != "application/json"
                    and not content_type.endswith("+json")
                    else MAX_RESPONSE_BYTES
                )
                raw = response.read(response_limit + 1)
                status_code = int(getattr(response, "status", 200))
                response_headers = response.headers
        except HTTPError as exc:
            raw = exc.read(MAX_RESPONSE_BYTES + 1)
            return _http_error(
                exc.code,
                raw,
                is_write=capability.method != "GET" and action_mode == "apply",
            )
        except (TimeoutError, URLError, OSError):
            if action_mode == "dry_run":
                return _error(
                    "store_owner_dry_run_failed",
                    request_dispatched=True,
                    outcome_uncertain=False,
                )
            return _error(
                "store_owner_outcome_uncertain" if capability.method != "GET" else "store_owner_read_failed",
                status="compensating" if capability.method != "GET" else "failed",
                outcome_uncertain=capability.method != "GET",
            )
        if len(raw) > response_limit:
            if action_mode == "dry_run":
                return _error(
                    "store_owner_dry_run_response_too_large",
                    request_dispatched=True,
                    outcome_uncertain=False,
                )
            if capability.method != "GET":
                return _uncertain_after_dispatch("store_owner_response_too_large")
            return _error("store_owner_response_too_large")
        if not 200 <= status_code < 300:
            return _http_error(
                status_code,
                raw,
                is_write=capability.method != "GET" and action_mode == "apply",
            )
        decoded = _decode_response(raw, content_type, allow_binary=allow_binary_response)
        if decoded.get("ok") is False:
            if action_mode == "dry_run":
                return _error(
                    str(decoded.get("error", {}).get("code") or "store_owner_dry_run_response_invalid"),
                    request_dispatched=True,
                    outcome_uncertain=False,
                )
            if capability.method != "GET":
                return _uncertain_after_dispatch(
                    str(decoded.get("error", {}).get("code") or "store_owner_response_invalid")
                )
            return decoded
        if _contains_sensitive_response_data(decoded.get("data")):
            return _sensitive_response_error(
                method=capability.method,
                action_mode=action_mode,
            )
        if action_mode == "dry_run":
            return _dry_run_result(
                decoded=decoded,
                response_headers=response_headers,
                expected_revision=expected_revision,
                planned_summary=planned_summary,
                request_content_type=payload["content_type"],
                status_code=status_code,
            )
        try:
            _validate_response_contract(
                capability,
                status_code=status_code,
                content_type=content_type,
                raw=raw,
                value=decoded.get("data"),
            )
        except ValueError as exc:
            if capability.method != "GET":
                return _uncertain_after_dispatch(str(exc))
            return _error(
                str(exc),
                request_dispatched=True,
                outcome_uncertain=False,
            )
        idempotency_replay = _idempotency_replay(response_headers)
        return {
            "ok": True,
            "format": STORE_OWNER_FORMAT,
            "status": "completed" if capability.method == "GET" else "compensating",
            "summary": {
                "operation_id": capability.operation_id,
                "method": capability.method,
                "path_template": capability.path_template,
                "risk": capability.risk,
                "http_status": status_code,
            },
            "data": decoded.get("data"),
            "meta": {
                "request_dispatched": True,
                "outcome_uncertain": False,
                "readback_required": capability.method != "GET",
                "write_applied": capability.method != "GET",
                "idempotency_replay": idempotency_replay,
                "verification": (
                    {
                        "status": "required",
                        "class": verification_class,
                        "target_ref": concrete_path,
                    }
                    if capability.method != "GET"
                    else {"status": "not_required"}
                ),
            },
        }


def _dry_run_result(
    *,
    decoded: dict[str, Any],
    response_headers: Any,
    expected_revision: str | None,
    planned_summary: dict[str, Any],
    request_content_type: str | None,
    status_code: int,
) -> dict[str, Any]:
    preflight = decoded.get("data")
    if not isinstance(preflight, dict):
        return _error("store_owner_dry_run_response_invalid", request_dispatched=True)
    proof = preflight.get("dryRunProof")
    receipt_id = preflight.get("receiptId")
    expires_at = preflight.get("expiresAt")
    response_proof = (
        str(response_headers.get(DRY_RUN_PROOF_HEADER) or "").strip() if hasattr(response_headers, "get") else ""
    )
    try:
        parsed_expiry = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        parsed_expiry = None
    now = datetime.now(UTC)
    expiry_is_valid = (
        parsed_expiry is not None
        and parsed_expiry.tzinfo is not None
        and parsed_expiry.astimezone(UTC) > now
        and parsed_expiry.astimezone(UTC) <= now + OWNER_PREFLIGHT_MAX_TTL
    )
    if (
        preflight.get("ok") is not True
        or preflight.get("mode") != "dry_run"
        or not isinstance(proof, str)
        or _OPAQUE_RECEIPT.fullmatch(proof) is None
        or response_proof != proof
        or not isinstance(receipt_id, str)
        or not 1 <= len(receipt_id) <= 120
        or not expiry_is_valid
        or preflight.get("currentRevision") != expected_revision
        or preflight.get("revisionKind") not in {"entity_updated_at", "revision_exempt", "route_opaque"}
        or preflight.get("contractVersion") != OWNER_PREFLIGHT_CONTRACT_VERSION
    ):
        return _error("store_owner_dry_run_response_invalid", request_dispatched=True)
    return {
        "ok": True,
        "format": STORE_OWNER_FORMAT,
        "status": "planned",
        "summary": {
            **planned_summary,
            "request_content_type": request_content_type,
            "dry_run_proof": proof,
            "server_receipt_id": receipt_id,
            "expires_at": str(expires_at),
            "current_revision": preflight.get("currentRevision"),
            "revision_kind": preflight.get("revisionKind"),
            "contract_version": preflight.get("contractVersion"),
            "http_status": status_code,
        },
        "meta": {
            "request_dispatched": True,
            "outcome_uncertain": False,
            "domain_handler_executed": False,
        },
        "data_included": False,
    }


def _store_origin(agent_api_url: str) -> str:
    configured = normalize_store_api_url(agent_api_url).rstrip("/")
    suffix = "/internal/agent/v1"
    if not configured.endswith(suffix):
        return ""
    return configured[: -len(suffix)]


def _is_employee_path(path: str) -> bool:
    return (
        path.startswith("/api/v1/")
        and not path.startswith("/api/v1/public/")
        and not path.startswith("/internal/")
        and len(path) <= 500
        and "?" not in path
        and "#" not in path
        and "\\" not in path
        and not any(ord(character) < 0x20 or ord(character) == 0x7F for character in path)
        and all(segment not in {".", ".."} for segment in path.split("/"))
    )


_JSON_SCHEMA_TYPES = frozenset({"array", "boolean", "integer", "null", "number", "object", "string"})
_JSON_SCHEMA_ANNOTATION_KEYS = frozenset(
    {
        "$comment",
        "$defs",
        "$id",
        "$schema",
        "contentEncoding",
        "contentMediaType",
        "default",
        "definitions",
        "deprecated",
        "description",
        "discriminator",
        "example",
        "examples",
        "externalDocs",
        "readOnly",
        "title",
        "writeOnly",
        "xml",
    }
)
_JSON_SCHEMA_MAPPING_KEYS = frozenset({"dependentSchemas", "patternProperties", "properties"})
_JSON_SCHEMA_SINGLE_SCHEMA_KEYS = frozenset(
    {
        "additionalProperties",
        "contains",
        "else",
        "if",
        "items",
        "not",
        "propertyNames",
        "then",
        "unevaluatedItems",
        "unevaluatedProperties",
    }
)
_JSON_SCHEMA_SCHEMA_LIST_KEYS = frozenset({"allOf", "anyOf", "oneOf", "prefixItems"})
_JSON_SCHEMA_NONNEGATIVE_INTEGER_KEYS = frozenset(
    {
        "maxContains",
        "maxItems",
        "maxLength",
        "maxProperties",
        "minContains",
        "minItems",
        "minLength",
        "minProperties",
    }
)
_JSON_SCHEMA_NUMBER_KEYS = frozenset({"exclusiveMaximum", "exclusiveMinimum", "maximum", "minimum", "multipleOf"})


class _InputSchemaSanitizer:
    """Return a bounded validation-only schema with self-contained local refs."""

    def __init__(self, root_schema: Any, *, components: dict[str, Any]) -> None:
        self.root_schema = root_schema
        self.components = components
        self.nodes = 0
        self.output_bytes = 0
        self.ref_keys: dict[tuple[str, str], str] = {}
        self.pending_refs: list[tuple[str, Any, Any, str]] = []

    def sanitize(self) -> bool | dict[str, Any]:
        sanitized = self._schema(
            self.root_schema,
            depth=0,
            scope_root=self.root_schema,
            scope_id="root",
        )
        definitions: dict[str, Any] = {}
        if self.pending_refs:
            if isinstance(sanitized, bool):
                sanitized = {"allOf": [sanitized]}
            sanitized["$defs"] = definitions
        position = 0
        while position < len(self.pending_refs):
            definition_key, target, scope_root, scope_id = self.pending_refs[position]
            position += 1
            definitions[definition_key] = self._schema(
                target,
                depth=0,
                scope_root=scope_root,
                scope_id=scope_id,
            )
            self._check_serialized_size(sanitized)
        if definitions:
            if isinstance(sanitized, bool):
                sanitized = {"allOf": [sanitized]}
            sanitized["$defs"] = {key: definitions[key] for key in sorted(definitions)}
        self._check_serialized_size(sanitized)
        try:
            Draft202012Validator.check_schema(sanitized)
        except (SchemaError, re.error) as exc:
            raise ValueError("store_owner_input_contract_schema_invalid") from exc
        return sanitized

    @staticmethod
    def _check_serialized_size(value: Any) -> None:
        if len(_canonical_json_bytes(value)) > MAX_INPUT_CONTRACT_BYTES:
            raise ValueError("store_owner_input_contract_too_large")

    def _bump(self, *, depth: int) -> None:
        if depth > MAX_INPUT_SCHEMA_DEPTH:
            raise ValueError("store_owner_input_contract_too_deep")
        self.nodes += 1
        if self.nodes > MAX_INPUT_SCHEMA_NODES:
            raise ValueError("store_owner_input_contract_too_complex")
        self._charge(())

    def _charge(self, value: Any) -> None:
        self.output_bytes += len(_canonical_json_bytes(value)) + 8
        if self.output_bytes > MAX_INPUT_CONTRACT_BYTES:
            raise ValueError("store_owner_input_contract_too_large")

    def _schema(
        self,
        value: Any,
        *,
        depth: int,
        scope_root: Any,
        scope_id: str,
    ) -> bool | dict[str, Any]:
        self._bump(depth=depth)
        if isinstance(value, bool):
            self._charge(value)
            return value
        if (
            not isinstance(value, dict)
            or len(value) > MAX_INPUT_SCHEMA_ITEMS
            or any(not isinstance(key, str) for key in value)
        ):
            raise ValueError("store_owner_input_contract_schema_invalid")
        output: dict[str, Any] = {}
        for key in sorted(value):
            item = value[key]
            if key in _JSON_SCHEMA_ANNOTATION_KEYS or key.startswith("x-"):
                continue
            self._charge(key)
            if key == "$ref":
                output[key] = self._register_local_ref(
                    item,
                    scope_root=scope_root,
                    scope_id=scope_id,
                )
            elif key in _JSON_SCHEMA_MAPPING_KEYS:
                output[key] = self._schema_mapping(
                    item,
                    depth=depth + 1,
                    scope_root=scope_root,
                    scope_id=scope_id,
                )
            elif key in _JSON_SCHEMA_SINGLE_SCHEMA_KEYS:
                output[key] = self._schema(
                    item,
                    depth=depth + 1,
                    scope_root=scope_root,
                    scope_id=scope_id,
                )
            elif key in _JSON_SCHEMA_SCHEMA_LIST_KEYS:
                output[key] = self._schema_list(
                    item,
                    depth=depth + 1,
                    scope_root=scope_root,
                    scope_id=scope_id,
                )
            elif key == "dependentRequired":
                output[key] = self._dependent_required(item)
            else:
                output[key] = self._validation_value(key, item)
        return output

    def _schema_mapping(
        self,
        value: Any,
        *,
        depth: int,
        scope_root: Any,
        scope_id: str,
    ) -> dict[str, Any]:
        if not isinstance(value, dict) or len(value) > MAX_INPUT_SCHEMA_ITEMS:
            raise ValueError("store_owner_input_contract_schema_invalid")
        output: dict[str, Any] = {}
        for raw_name, schema in sorted(value.items(), key=lambda pair: str(pair[0])):
            name = self._name(raw_name)
            self._charge(name)
            output[name] = self._schema(
                schema,
                depth=depth,
                scope_root=scope_root,
                scope_id=scope_id,
            )
        return output

    def _schema_list(
        self,
        value: Any,
        *,
        depth: int,
        scope_root: Any,
        scope_id: str,
    ) -> list[Any]:
        if not isinstance(value, list) or not value or len(value) > MAX_INPUT_SCHEMA_ITEMS:
            raise ValueError("store_owner_input_contract_schema_invalid")
        return [
            self._schema(
                item,
                depth=depth,
                scope_root=scope_root,
                scope_id=scope_id,
            )
            for item in value
        ]

    def _dependent_required(self, value: Any) -> dict[str, list[str]]:
        if not isinstance(value, dict) or len(value) > MAX_INPUT_SCHEMA_ITEMS:
            raise ValueError("store_owner_input_contract_schema_invalid")
        return {
            self._name(raw_name): self._string_list(names)
            for raw_name, names in sorted(value.items(), key=lambda pair: str(pair[0]))
        }

    def _string_list(self, value: Any) -> list[str]:
        if not isinstance(value, list) or len(value) > MAX_INPUT_SCHEMA_ITEMS:
            raise ValueError("store_owner_input_contract_schema_invalid")
        normalized = [self._name(item) for item in value]
        if len(set(normalized)) != len(normalized):
            raise ValueError("store_owner_input_contract_schema_invalid")
        self._charge(normalized)
        return normalized

    def _schema_type(self, value: Any) -> str | list[str]:
        if isinstance(value, str):
            if value not in _JSON_SCHEMA_TYPES:
                raise ValueError("store_owner_input_contract_schema_invalid")
            self._charge(value)
            return value
        values = self._string_list(value)
        if not values or any(item not in _JSON_SCHEMA_TYPES for item in values):
            raise ValueError("store_owner_input_contract_schema_invalid")
        return sorted(values)

    def _enum(self, value: Any) -> list[Any]:
        if not isinstance(value, list) or not value or len(value) > MAX_INPUT_SCHEMA_ITEMS:
            raise ValueError("store_owner_input_contract_schema_invalid")
        result = [self._literal(item) for item in value]
        canonical = [_canonical_json_bytes(item) for item in result]
        if len(set(canonical)) != len(canonical):
            raise ValueError("store_owner_input_contract_schema_invalid")
        self._charge(result)
        return result

    def _validation_value(self, key: str, value: Any) -> Any:
        if key == "required":
            return self._string_list(value)
        if key == "type":
            return self._schema_type(value)
        if key in {"const", "enum"}:
            result = self._enum(value) if key == "enum" else self._literal(value)
        elif key in _JSON_SCHEMA_NONNEGATIVE_INTEGER_KEYS:
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError("store_owner_input_contract_schema_invalid")
            result = value
        elif key in _JSON_SCHEMA_NUMBER_KEYS:
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError("store_owner_input_contract_schema_invalid")
            result = value
        elif key == "uniqueItems":
            if not isinstance(value, bool):
                raise ValueError("store_owner_input_contract_schema_invalid")
            result = value
        elif key in {"format", "pattern"}:
            result = self._string(value)
        else:
            raise ValueError("store_owner_input_contract_keyword_unsupported")
        if key != "enum":
            self._charge(result)
        return result

    def _literal(self, value: Any) -> Any:
        if value is None or isinstance(value, bool):
            return value
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        if isinstance(value, float) and math.isfinite(value):
            return value
        if isinstance(value, str):
            return self._string(value)
        raise ValueError("store_owner_input_contract_literal_invalid")

    def _string(self, value: Any) -> str:
        if not isinstance(value, str) or len(value) > MAX_INPUT_SCHEMA_STRING_LENGTH:
            raise ValueError("store_owner_input_contract_schema_invalid")
        if any(ord(character) < 0x20 and character not in "\t\n\r" for character in value):
            raise ValueError("store_owner_input_contract_schema_invalid")
        return value

    def _name(self, value: Any) -> str:
        name = self._string(value)
        if not name or len(name) > 200:
            raise ValueError("store_owner_input_contract_schema_invalid")
        return name

    def _register_local_ref(
        self,
        value: Any,
        *,
        scope_root: Any,
        scope_id: str,
    ) -> str:
        reference = self._string(value)
        if reference != "#" and not reference.startswith("#/"):
            raise ValueError("store_owner_input_contract_ref_invalid")
        parts = _json_pointer_parts(reference)
        if parts[:2] == ["components", "schemas"]:
            if len(parts) < 3:
                raise ValueError("store_owner_input_contract_ref_invalid")
            schemas = self.components.get("schemas")
            if not isinstance(schemas, dict) or parts[2] not in schemas:
                raise ValueError("store_owner_input_contract_ref_invalid")
            target_scope = schemas[parts[2]]
            target = self._resolve_pointer({"components": self.components}, parts)
            target_scope_id = f"component:{hashlib.sha256(parts[2].encode()).hexdigest()}"
            reference_scope = "document"
        elif parts and parts[0] == "components":
            raise ValueError("store_owner_input_contract_ref_invalid")
        else:
            target_scope = scope_root
            target = self._resolve_pointer(scope_root, parts)
            target_scope_id = scope_id
            reference_scope = scope_id
        if not isinstance(target_scope, (bool, dict)) or not isinstance(target, (bool, dict)):
            raise ValueError("store_owner_input_contract_ref_invalid")
        ref_key = (reference_scope, reference)
        if ref_key not in self.ref_keys:
            if len(self.ref_keys) >= MAX_INPUT_SCHEMA_DEFINITIONS:
                raise ValueError("store_owner_input_contract_too_complex")
            digest_input = f"{reference_scope}\0{reference}".encode()
            definition_key = f"schema_{hashlib.sha256(digest_input).hexdigest()[:24]}"
            self.ref_keys[ref_key] = definition_key
            if len(self.ref_keys) == 1:
                self._charge("$defs")
            self._charge(definition_key)
            self.pending_refs.append((definition_key, target, target_scope, target_scope_id))
        rendered = f"#/$defs/{self.ref_keys[ref_key]}"
        self._charge(rendered)
        return rendered

    @staticmethod
    def _resolve_pointer(root: Any, parts: list[str]) -> Any:
        for part in parts:
            if isinstance(root, dict) and part in root:
                root = root[part]
            elif (
                isinstance(root, list)
                and (part == "0" or (part.isdigit() and not part.startswith("0")))
                and int(part) < len(root)
            ):
                root = root[int(part)]
            else:
                raise ValueError("store_owner_input_contract_ref_invalid")
        return root


def _json_pointer_parts(reference: str) -> list[str]:
    if reference == "#":
        return []
    if not reference.startswith("#/"):
        raise ValueError("store_owner_input_contract_ref_invalid")
    parts: list[str] = []
    for raw_part in reference[2:].split("/"):
        if re.search(r"~(?![01])", raw_part):
            raise ValueError("store_owner_input_contract_ref_invalid")
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if not part or len(part) > 200 or any(ord(character) < 0x20 for character in part):
            raise ValueError("store_owner_input_contract_ref_invalid")
        parts.append(part)
    if len(parts) > MAX_INPUT_SCHEMA_DEPTH:
        raise ValueError("store_owner_input_contract_ref_invalid")
    return parts


def _parameter_schema(
    names: tuple[str, ...],
    schemas: tuple[tuple[str, Any], ...],
    *,
    required: tuple[str, ...],
    components: dict[str, Any],
) -> bool | dict[str, Any]:
    declared = [str(name) for name in names]
    schema_by_name = {str(name): schema for name, schema in schemas if str(name) in declared}
    raw_schema = {
        "type": "object",
        "properties": {name: schema_by_name.get(name, True) for name in declared},
        "required": list(required),
        "additionalProperties": False,
    }
    return _InputSchemaSanitizer(raw_schema, components=components).sanitize()


def _operation_input_contract(capability: OwnerCapability) -> dict[str, Any]:
    contract = {
        "contract_version": "store-owner-input-contract-v1",
        "path_parameters": _parameter_schema(
            capability.path_parameters,
            capability.path_parameter_schemas,
            required=capability.path_parameters,
            components=capability.schema_components,
        ),
        "query_parameters": _parameter_schema(
            capability.query_parameters,
            capability.query_parameter_schemas,
            required=capability.required_query_parameters,
            components=capability.schema_components,
        ),
        "request_body": {
            "required": capability.request_required,
            "content": [
                {
                    "content_type": content_type,
                    "schema": _InputSchemaSanitizer(
                        schema,
                        components=capability.schema_components,
                    ).sanitize(),
                }
                for content_type, schema in sorted(capability.request_schemas, key=lambda item: item[0])
            ],
        },
    }
    if len(_canonical_json_bytes(contract)) > MAX_INPUT_CONTRACT_BYTES:
        raise ValueError("store_owner_input_contract_too_large")
    return contract


def _risk(method: str, path: str) -> str:
    if method == "GET":
        return "read"
    if (method, path) in _SESSION_BOUNDARIES:
        return "write"
    # The generic owner route is broader than the seven named Store writes.
    # A path alone cannot prove that a body is free of pricing, customer,
    # publication, stock, or downstream marketplace effects, so fail closed.
    return "high_risk_write"


def is_safe_reversible_collection_create(method: str, path: str) -> bool:
    """Return true only for reviewed creates that have no prior entity revision."""
    return method.upper() == "POST" and path in SAFE_REVERSIBLE_COLLECTION_CREATE_PATHS


def _expected_revision_required(capability: OwnerCapability) -> bool:
    return capability.method != "GET" and not is_safe_reversible_collection_create(
        capability.method,
        capability.path_template,
    )


def _validated_expected_revision(
    capability: OwnerCapability,
    value: str | None,
    *,
    enforce_required: bool,
) -> str | None:
    normalized = str(value or "").strip()
    if enforce_required and _expected_revision_required(capability) and not normalized:
        raise ValueError("store_owner_expected_revision_required")
    if normalized and (
        len(normalized) > 200
        or not normalized.isascii()
        or any(ord(character) < 0x20 or ord(character) > 0x7E for character in normalized)
    ):
        raise ValueError("store_owner_expected_revision_invalid")
    return normalized or None


def _verification_class(capability: OwnerCapability) -> str:
    if capability.method == "DELETE":
        return "absence_plus_audit"
    if is_safe_reversible_collection_create(capability.method, capability.path_template):
        return "collection_membership"
    if capability.method in {"PUT", "PATCH"} and capability.path_parameters:
        return "exact_entity"
    return "operation_specific_state"


def _binary_response_allowed(capability: OwnerCapability) -> bool:
    return capability.method == "GET" and any(
        media_type != "application/json" and not media_type.endswith("+json")
        for media_type in capability.response_content_types
    )


def _validate_response_contract(
    capability: OwnerCapability,
    *,
    status_code: int,
    content_type: str,
    raw: bytes,
    value: Any,
) -> None:
    if not capability.response_contract_enforced:
        return
    status = str(status_code)
    matching_statuses = {
        candidate for candidate in capability.response_statuses if candidate == status or candidate == f"{status[0]}XX"
    }
    if not matching_statuses:
        raise ValueError("store_owner_response_status_invalid")
    matching_content_types = {
        media_type
        for schema_status, media_type in capability.response_content_contracts
        if schema_status in matching_statuses
    }
    matching_schemas = [
        schema
        for schema_status, media_type, schema in capability.response_schemas
        if schema_status in matching_statuses and media_type == content_type
    ]
    if not raw:
        if matching_content_types:
            raise ValueError("store_owner_response_schema_invalid")
        return
    if content_type not in matching_content_types:
        raise ValueError("store_owner_response_content_type_invalid")
    if content_type != "application/json" and not content_type.endswith("+json"):
        return
    if len(matching_schemas) != 1:
        raise ValueError("store_owner_response_schema_missing")
    _validate_schema_value(
        value,
        schema=matching_schemas[0],
        components=capability.schema_components,
        error_code="store_owner_response_schema_invalid",
    )


def _parse_capabilities(openapi: dict[str, Any]) -> dict[str, OwnerCapability]:
    paths = openapi.get("paths")
    if not isinstance(paths, dict) or len(paths) > 500:
        raise ValueError("openapi_paths_invalid")
    capabilities: dict[str, OwnerCapability] = {}
    raw_components = openapi.get("components")
    components: dict[str, Any] = raw_components if isinstance(raw_components, dict) else {}
    components_hash = _canonical_hash(components)
    for path_template, path_item in paths.items():
        if not isinstance(path_template, str) or not isinstance(path_item, dict):
            continue
        if not _is_employee_path(path_template):
            continue
        for method, operation in path_item.items():
            normalized_method = str(method).casefold()
            if normalized_method not in _HTTP_METHODS or not isinstance(operation, dict):
                continue
            method_upper = normalized_method.upper()
            if (method_upper, path_template) in _SESSION_BOUNDARIES:
                continue
            operation_id = str(operation.get("operationId") or "").strip()
            if _OPERATION_ID.fullmatch(operation_id) is None or operation_id in capabilities:
                raise ValueError("openapi_operation_id_invalid")
            raw_request_body = operation.get("requestBody")
            request_body = _resolved_openapi_object(openapi, raw_request_body)
            raw_content = request_body.get("content")
            content: dict[str, Any] = raw_content if isinstance(raw_content, dict) else {}
            request_schema_map = {
                _normalized_media_type(media_type): media.get("schema")
                for media_type, media in content.items()
                if _normalized_media_type(media_type)
                and isinstance(media, dict)
                and isinstance(media.get("schema"), (dict, bool))
            }
            request_schemas = tuple(sorted(request_schema_map.items(), key=lambda item: item[0]))
            parameters = _operation_parameters(openapi, path_item, operation)
            query_parameters = tuple(
                sorted(
                    str(parameter.get("name") or "")
                    for parameter in parameters
                    if parameter.get("in") == "query" and str(parameter.get("name") or "")
                )
            )
            required_query_parameters = tuple(
                sorted(
                    str(parameter.get("name") or "")
                    for parameter in parameters
                    if parameter.get("in") == "query"
                    and bool(parameter.get("required"))
                    and str(parameter.get("name") or "")
                )
            )
            path_parameter_schemas = tuple(
                sorted(
                    (
                        str(parameter.get("name") or ""),
                        parameter.get("schema"),
                    )
                    for parameter in parameters
                    if parameter.get("in") == "path"
                    and str(parameter.get("name") or "")
                    and isinstance(parameter.get("schema"), (dict, bool))
                )
            )
            query_parameter_schemas = tuple(
                sorted(
                    (
                        str(parameter.get("name") or ""),
                        parameter.get("schema"),
                    )
                    for parameter in parameters
                    if parameter.get("in") == "query"
                    and str(parameter.get("name") or "")
                    and isinstance(parameter.get("schema"), (dict, bool))
                )
            )
            (
                response_statuses,
                response_content_types,
                response_content_contracts,
                response_schemas,
            ) = _response_contracts(openapi, operation)
            capabilities[operation_id] = OwnerCapability(
                operation_id=operation_id,
                method=method_upper,
                path_template=path_template,
                risk=_risk(method_upper, path_template),
                request_content_types=tuple(sorted(filter(None, (_normalized_media_type(item) for item in content)))),
                request_required=bool(request_body.get("required")),
                path_parameters=tuple(_PATH_PARAMETER.findall(path_template)),
                schema_hash=_canonical_hash(
                    {
                        "components_hash": components_hash,
                        "method": method_upper,
                        "operation": operation,
                        "path": path_template,
                    }
                ),
                query_parameters=query_parameters,
                required_query_parameters=required_query_parameters,
                request_schemas=request_schemas,
                response_content_types=response_content_types,
                response_statuses=response_statuses,
                response_content_contracts=response_content_contracts,
                response_schemas=response_schemas,
                response_contract_enforced=True,
                path_parameter_schemas=path_parameter_schemas,
                query_parameter_schemas=query_parameter_schemas,
                schema_components=components,
            )
    if len(capabilities) < 100 or len(capabilities) > MAX_CAPABILITIES:
        raise ValueError("openapi_employee_capability_count_invalid")
    return capabilities


def _concrete_path(capability: OwnerCapability, values: dict[str, Any]) -> str:
    expected = set(capability.path_parameters)
    actual = {str(key) for key in values}
    if expected != actual:
        raise ValueError("store_owner_path_parameters_mismatch")
    path = capability.path_template
    schemas = dict(capability.path_parameter_schemas)
    for name in capability.path_parameters:
        raw_value = values.get(name)
        if name in schemas:
            _validate_schema_value(
                raw_value,
                schema=schemas[name],
                components=capability.schema_components,
                error_code="store_owner_path_parameter_schema_invalid",
            )
        value = str(raw_value or "").strip()
        if (
            not value
            or len(value) > 200
            or value in {".", ".."}
            or "/" in value
            or "\\" in value
            or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
        ):
            raise ValueError("store_owner_path_parameter_invalid")
        path = path.replace(f"{{{name}}}", quote(value, safe=""))
    return path


def _validated_mapping(value: dict[str, Any] | None, *, name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict) or len(value) > 100:
        raise ValueError(f"store_owner_{name}_invalid")
    _validate_json_value(value)
    return dict(value)


def _validated_query(value: dict[str, Any] | None, *, capability: OwnerCapability) -> dict[str, Any]:
    query = _validated_mapping(value, name="query")
    allowed = set(capability.query_parameters)
    if set(query).difference(allowed) or set(capability.required_query_parameters).difference(query):
        raise ValueError("store_owner_query_parameters_mismatch")
    schemas = dict(capability.query_parameter_schemas)
    for name, item in query.items():
        if item is None or isinstance(item, (str, int, float, bool)):
            pass
        elif isinstance(item, list) and all(
            value is None or isinstance(value, (str, int, float, bool)) for value in item
        ):
            pass
        else:
            raise ValueError("store_owner_query_invalid")
        if name in schemas:
            _validate_schema_value(
                item,
                schema=schemas[name],
                components=capability.schema_components,
                error_code="store_owner_query_schema_invalid",
            )
    normalized = {name: query[name] for name in sorted(query)}
    if len(urlencode(normalized, doseq=True).encode("utf-8")) > MAX_QUERY_BYTES:
        raise ValueError("store_owner_query_too_large")
    return normalized


def _operation_parameters(
    openapi: dict[str, Any], path_item: dict[str, Any], operation: dict[str, Any]
) -> list[dict[str, Any]]:
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for source in (path_item.get("parameters"), operation.get("parameters")):
        if not isinstance(source, list):
            continue
        for raw_parameter in source:
            parameter = _resolved_openapi_object(openapi, raw_parameter)
            if not parameter:
                continue
            location = str(parameter.get("in") or "")
            name = str(parameter.get("name") or "")
            if location and name:
                merged[(location, name)] = parameter
    return list(merged.values())


def _resolved_openapi_object(openapi: dict[str, Any], value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    current = dict(value)
    seen: set[str] = set()
    for _ in range(8):
        reference = current.get("$ref")
        if not isinstance(reference, str):
            return current
        if reference in seen or not reference.startswith("#/components/"):
            return {}
        seen.add(reference)
        resolved: Any = openapi
        try:
            for token in reference[2:].split("/"):
                decoded = token.replace("~1", "/").replace("~0", "~")
                resolved = resolved[decoded]
        except (KeyError, TypeError):
            return {}
        if not isinstance(resolved, dict):
            return {}
        siblings = {key: item for key, item in current.items() if key != "$ref"}
        current = {**resolved, **siblings}
    return {}


def _normalized_media_type(value: Any) -> str:
    media_type = str(value or "").split(";", 1)[0].strip().casefold()
    return media_type if _MEDIA_TYPE.fullmatch(media_type) is not None else ""


def _response_contracts(
    openapi: dict[str, Any], operation: dict[str, Any]
) -> tuple[
    tuple[str, ...],
    tuple[str, ...],
    tuple[tuple[str, str], ...],
    tuple[tuple[str, str, Any], ...],
]:
    raw_responses = operation.get("responses")
    responses: dict[str, Any] = raw_responses if isinstance(raw_responses, dict) else {}
    statuses: set[str] = set()
    media_types: set[str] = set()
    content_contracts: set[tuple[str, str]] = set()
    schemas: list[tuple[str, str, Any]] = []
    for status, raw_response in responses.items():
        normalized_status = str(status or "").strip().upper()
        if not normalized_status.startswith("2"):
            continue
        statuses.add(normalized_status)
        response = _resolved_openapi_object(openapi, raw_response)
        content = response.get("content")
        if not isinstance(content, dict):
            continue
        for raw_media_type, media in content.items():
            media_type = _normalized_media_type(raw_media_type)
            if not media_type:
                continue
            media_types.add(media_type)
            content_contracts.add((normalized_status, media_type))
            if isinstance(media, dict) and isinstance(media.get("schema"), (dict, bool)):
                schemas.append((normalized_status, media_type, media.get("schema")))
    schemas.sort(key=lambda item: (item[0], item[1]))
    return (
        tuple(sorted(statuses)),
        tuple(sorted(media_types)),
        tuple(sorted(content_contracts)),
        tuple(schemas),
    )


def _validate_schema_value(
    value: Any,
    *,
    schema: Any,
    components: dict[str, Any],
    error_code: str,
) -> None:
    if not isinstance(schema, (dict, bool)):
        raise ValueError("store_owner_openapi_schema_missing")
    validation_root = {
        "$schema": _JSON_SCHEMA_DIALECT,
        "components": components,
        "allOf": [schema],
    }
    try:
        Draft202012Validator.check_schema(validation_root)
        validator = Draft202012Validator(validation_root, format_checker=FormatChecker())
        if next(validator.iter_errors(value), None) is not None:
            raise ValueError(error_code)
    except (SchemaError, Unresolvable, re.error) as exc:
        raise ValueError("store_owner_openapi_schema_invalid") from exc


def _request_schema(capability: OwnerCapability, media_type: str) -> Any:
    for candidate, schema in capability.request_schemas:
        if candidate == media_type:
            return schema
    return None


def _validate_request_schema(capability: OwnerCapability, *, media_type: str, value: Any) -> None:
    schema = _request_schema(capability, media_type)
    _validate_schema_value(
        value,
        schema=schema,
        components=capability.schema_components,
        error_code="store_owner_request_schema_invalid",
    )


def _validate_multipart_schema(
    capability: OwnerCapability,
    *,
    form: dict[str, Any],
    files: list[dict[str, Any]],
) -> None:
    schema = _request_schema(capability, "multipart/form-data")
    if not isinstance(schema, (dict, bool)):
        raise ValueError("store_owner_openapi_schema_missing")
    instance = dict(form)
    grouped: dict[str, list[str]] = {}
    for item in files:
        if not isinstance(item, dict):
            raise ValueError("store_owner_files_invalid")
        field_name = str(item.get("field") or "").strip()
        grouped.setdefault(field_name, []).append(str(item.get("filename") or "file"))
    for field_name, placeholders in grouped.items():
        property_schema = _schema_property(schema, field_name, capability.schema_components)
        instance[field_name] = (
            placeholders if _schema_accepts_array(property_schema, capability.schema_components) else placeholders[0]
        )
        if len(placeholders) > 1 and not _schema_accepts_array(property_schema, capability.schema_components):
            raise ValueError("store_owner_request_schema_invalid")
    _validate_schema_value(
        instance,
        schema=schema,
        components=capability.schema_components,
        error_code="store_owner_request_schema_invalid",
    )


def _schema_property(schema: Any, name: str, components: dict[str, Any], *, depth: int = 0) -> Any:
    if depth > 12 or not isinstance(schema, dict):
        return None
    resolved = _resolved_schema_reference(schema, components)
    properties = resolved.get("properties")
    if isinstance(properties, dict) and name in properties:
        return properties[name]
    for keyword in ("allOf", "anyOf", "oneOf"):
        branches = resolved.get(keyword)
        if isinstance(branches, list):
            for branch in branches:
                found = _schema_property(branch, name, components, depth=depth + 1)
                if found is not None:
                    return found
    return None


def _resolved_schema_reference(schema: dict[str, Any], components: dict[str, Any]) -> dict[str, Any]:
    reference = schema.get("$ref")
    prefix = "#/components/schemas/"
    if not isinstance(reference, str) or not reference.startswith(prefix):
        return schema
    schemas = components.get("schemas")
    if not isinstance(schemas, dict):
        return schema
    name = reference[len(prefix) :].replace("~1", "/").replace("~0", "~")
    resolved = schemas.get(name)
    return resolved if isinstance(resolved, dict) else schema


def _schema_accepts_array(schema: Any, components: dict[str, Any], *, depth: int = 0) -> bool:
    if depth > 12 or not isinstance(schema, dict):
        return False
    resolved = _resolved_schema_reference(schema, components)
    schema_type = resolved.get("type")
    if schema_type == "array" or (isinstance(schema_type, list) and "array" in schema_type):
        return True
    for keyword in ("allOf", "anyOf", "oneOf"):
        branches = resolved.get(keyword)
        if isinstance(branches, list) and any(
            _schema_accepts_array(branch, components, depth=depth + 1) for branch in branches
        ):
            return True
    return False


def _validate_json_value(value: Any, *, depth: int = 0) -> None:
    if depth > MAX_JSON_DEPTH:
        raise ValueError("store_owner_payload_too_deep")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("store_owner_payload_invalid")
    if value is None or isinstance(value, (str, int, float, bool)):
        return
    if isinstance(value, dict):
        if len(value) > MAX_CONTAINER_ITEMS or any(not isinstance(key, str) for key in value):
            raise ValueError("store_owner_payload_invalid")
        for item in value.values():
            _validate_json_value(item, depth=depth + 1)
        return
    if isinstance(value, list):
        if len(value) > MAX_CONTAINER_ITEMS:
            raise ValueError("store_owner_payload_invalid")
        for item in value:
            _validate_json_value(item, depth=depth + 1)
        return
    raise ValueError("store_owner_payload_invalid")


def _request_payload(
    *,
    capability: OwnerCapability,
    body: Any,
    form: dict[str, Any],
    files: list[dict[str, Any]],
) -> dict[str, Any]:
    if body is not None and (form or files):
        raise ValueError("store_owner_request_body_modes_conflict")
    if files or form:
        if not any(item.startswith("multipart/form-data") for item in capability.request_content_types):
            raise ValueError("store_owner_multipart_not_supported")
        _validate_multipart_schema(capability, form=form, files=files)
        data, content_type, file_metadata, canonical_sha256 = _multipart_payload(form=form, files=files)
        field_names = sorted(form)
    elif body is not None:
        if "application/json" not in capability.request_content_types:
            raise ValueError("store_owner_json_not_supported")
        _validate_request_schema(capability, media_type="application/json", value=body)
        data = json.dumps(
            body,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        if len(data) > MAX_REQUEST_BYTES:
            raise ValueError("store_owner_request_too_large")
        content_type = "application/json"
        field_names = sorted(body) if isinstance(body, dict) else []
        file_metadata = []
        canonical_sha256 = hashlib.sha256(data).hexdigest()
    else:
        data = None
        content_type = ""
        field_names = []
        file_metadata = []
        canonical_sha256 = hashlib.sha256(b"").hexdigest()
    if capability.request_required and data is None:
        raise ValueError("store_owner_request_body_required")
    return {
        "data": data,
        "content_type": content_type,
        "field_names": field_names,
        "file_metadata": file_metadata,
        "canonical_sha256": canonical_sha256,
    }


def _multipart_payload(
    *, form: dict[str, Any], files: list[dict[str, Any]]
) -> tuple[bytes, str, list[dict[str, Any]], str]:
    if len(files) > 20:
        raise ValueError("store_owner_files_invalid")
    form_parts: list[tuple[str, str]] = []
    metadata: list[dict[str, Any]] = []
    normalized_files: list[tuple[str, str, str, bytes]] = []
    total = 0
    form_bytes = 0
    for name, value in sorted(form.items()):
        if isinstance(value, (dict, list)):
            rendered = json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        elif value is None:
            rendered = ""
        else:
            rendered = str(value)
        normalized_name = _header_token(name)
        form_bytes += len(normalized_name.encode("utf-8")) + len(rendered.encode("utf-8"))
        if form_bytes > MAX_REQUEST_BYTES:
            raise ValueError("store_owner_request_too_large")
        form_parts.append((normalized_name, rendered))
    encoded_total = sum(len(str(item.get("content_base64") or "")) for item in files if isinstance(item, dict))
    encoded_ceiling = ((MAX_FILES_BYTES + 2) // 3) * 4 + len(files) * 4
    if encoded_total > encoded_ceiling:
        raise ValueError("store_owner_files_too_large")
    for item in files:
        if not isinstance(item, dict):
            raise ValueError("store_owner_files_invalid")
        field = _header_token(str(item.get("field") or ""))
        filename = _header_token(str(item.get("filename") or ""))
        content_type = str(item.get("content_type") or "application/octet-stream").strip().casefold()
        encoded = str(item.get("content_base64") or "")
        if (
            not field
            or not filename
            or _MEDIA_TYPE.fullmatch(content_type) is None
            or len(encoded) > MAX_FILE_BYTES * 2
        ):
            raise ValueError("store_owner_files_invalid")
        try:
            content = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError) as exc:
            raise ValueError("store_owner_files_invalid") from exc
        if not content or len(content) > MAX_FILE_BYTES:
            raise ValueError("store_owner_file_too_large")
        total += len(content)
        if total > MAX_FILES_BYTES:
            raise ValueError("store_owner_files_too_large")
        normalized_files.append((field, filename, content_type, content))
        metadata.append(
            {
                "field": field,
                "filename": filename,
                "content_type": content_type,
                "byte_size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        )

    canonical_sha256 = _canonical_hash({"files": metadata, "form": form})
    boundary = _stable_multipart_boundary(
        canonical_sha256=canonical_sha256,
        form_parts=form_parts,
        files=normalized_files,
    )
    chunks: list[bytes] = []
    for name, rendered in form_parts:
        chunks.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{rendered}\r\n').encode())
    for field, filename, content_type, content in normalized_files:
        chunks.extend(
            [
                (
                    f"--{boundary}\r\n"
                    f'Content-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'
                    f"Content-Type: {content_type}\r\n\r\n"
                ).encode(),
                content,
                b"\r\n",
            ]
        )
    chunks.append(f"--{boundary}--\r\n".encode("ascii"))
    data = b"".join(chunks)
    if len(data) > MAX_FILES_BYTES + MAX_REQUEST_BYTES:
        raise ValueError("store_owner_request_too_large")
    return data, f"multipart/form-data; boundary={boundary}", metadata, canonical_sha256


def _stable_multipart_boundary(
    *,
    canonical_sha256: str,
    form_parts: list[tuple[str, str]],
    files: list[tuple[str, str, str, bytes]],
) -> str:
    """Build byte-stable multipart requests so idempotent retries can replay."""
    collision_material = [f"{name}\0{rendered}".encode() for name, rendered in form_parts]
    collision_material.extend(
        f"{field}\0{filename}\0{content_type}".encode() + content for field, filename, content_type, content in files
    )
    for attempt in range(100):
        seed = hashlib.sha256(f"{canonical_sha256}:{attempt}".encode("ascii")).hexdigest()
        boundary = f"autostop-{seed[:32]}"
        token = boundary.encode("ascii")
        if all(token not in item for item in collision_material):
            return boundary
    raise ValueError("store_owner_multipart_boundary_unavailable")


def _header_token(value: str) -> str:
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > 200
        or '"' in normalized
        or "\\" in normalized
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in normalized)
    ):
        raise ValueError("store_owner_multipart_header_invalid")
    return normalized


def _plan_hash(
    *,
    capability: OwnerCapability,
    concrete_path: str,
    query: dict[str, Any],
    payload: dict[str, Any],
    expected_revision: str | None,
) -> str:
    canonical = {
        "operation_id": capability.operation_id,
        "method": capability.method,
        "path": concrete_path,
        "query": query,
        "request_sha256": payload["canonical_sha256"],
        "schema_hash": capability.schema_hash,
        "expected_revision": str(expected_revision or "").strip() or None,
    }
    encoded = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _canonical_hash(value: Any) -> str:
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _validate_write_metadata(*, owner_intent: str, idempotency_key: str, correlation_id: str) -> str | None:
    try:
        _owner_intent_header_value(owner_intent)
    except ValueError as exc:
        return str(exc)
    if _IDENTIFIER.fullmatch(str(idempotency_key or "").strip()) is None:
        return "store_owner_idempotency_key_invalid"
    if _IDENTIFIER.fullmatch(str(correlation_id or "").strip()) is None:
        return "store_owner_correlation_id_invalid"
    return None


def _owner_intent_header_value(value: str) -> str:
    normalized = str(value or "").strip()
    if (
        not normalized
        or len(normalized) > 500
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in normalized)
    ):
        raise ValueError("store_owner_intent_invalid")
    if normalized.isascii():
        return normalized
    encoded = base64.urlsafe_b64encode(normalized.encode("utf-8")).decode("ascii").rstrip("=")
    header_value = f"utf8-b64:{encoded}"
    if len(header_value) > 500:
        raise ValueError("store_owner_intent_invalid")
    return header_value


def _decode_response(raw: bytes, content_type: str, *, allow_binary: bool) -> dict[str, Any]:
    if not raw:
        return {"ok": True, "data": None}
    if content_type == "application/json" or content_type.endswith("+json"):
        try:
            value = json.loads(raw.decode("utf-8"))
            _validate_json_value(value)
        except (UnicodeError, ValueError, TypeError):
            return _error("store_owner_response_invalid")
        if not allow_binary and _contains_embedded_binary(value):
            return _error("store_owner_binary_response_not_allowed")
        return {"ok": True, "data": value}
    if not allow_binary:
        return {
            "ok": True,
            "data": {
                "content_type": content_type or "application/octet-stream",
                "byte_size": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "content_omitted": True,
            },
        }
    return {
        "ok": True,
        "data": {
            "content_type": content_type or "application/octet-stream",
            "byte_size": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "content_base64": base64.b64encode(raw).decode("ascii"),
        },
    }


def _contains_embedded_binary(value: Any, *, depth: int = 0) -> bool:
    if depth > MAX_JSON_DEPTH:
        return True
    if isinstance(value, dict):
        binary_keys = {
            "content_base64",
            "data_base64",
            "file_base64",
            "file_bytes",
            "raw_bytes",
        }
        if any(str(key).casefold() in binary_keys for key in value):
            return True
        return any(_contains_embedded_binary(item, depth=depth + 1) for item in value.values())
    if isinstance(value, list):
        return any(_contains_embedded_binary(item, depth=depth + 1) for item in value)
    return False


def _contains_sensitive_response_data(value: Any, *, depth: int = 0) -> bool:
    """Fail closed before an employee response can leave the owner transport."""
    if depth > MAX_JSON_DEPTH:
        return True
    if isinstance(value, dict):
        exact_sensitive_keys = {
            "access_token",
            "apikey",
            "api_key",
            "authorization",
            "credential",
            "key1",
            "key2",
            "password",
            "private_key",
            "refresh_token",
            "secret",
            "secret_key",
            "token",
        }
        for raw_key, nested in value.items():
            camel_split = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", str(raw_key))
            camel_split = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", camel_split)
            key = re.sub(r"[^a-z0-9]+", "_", camel_split.casefold()).strip("_")
            key_is_sensitive = key in exact_sensitive_keys or key.endswith(
                (
                    "_api_key",
                    "_credential",
                    "_password",
                    "_private_key",
                    "_secret",
                    "_secret_key",
                    "_token",
                )
            )
            if key_is_sensitive and nested is not None:
                return True
            if _contains_sensitive_response_data(nested, depth=depth + 1):
                return True
        return False
    if isinstance(value, list):
        return any(_contains_sensitive_response_data(item, depth=depth + 1) for item in value)
    return False


def _sensitive_response_error(*, method: str, action_mode: str) -> dict[str, Any]:
    if action_mode == "dry_run" or method == "GET":
        return _error(
            "store_owner_sensitive_response_blocked",
            request_dispatched=True,
            outcome_uncertain=False,
        )
    return _uncertain_after_dispatch("store_owner_sensitive_response_blocked")


def _idempotency_replay(headers: Any) -> bool:
    for name in (
        "Idempotency-Replayed",
        "X-Idempotency-Replay",
        "X-Autostop-Idempotency-Replay",
    ):
        value = str(headers.get(name) or "").strip().casefold() if hasattr(headers, "get") else ""
        if value in {"1", "true", "yes"}:
            return True
    return False


def _uncertain_after_dispatch(code: str, **metadata: Any) -> dict[str, Any]:
    return _error(
        code,
        status="compensating",
        request_dispatched=True,
        outcome_uncertain=True,
        readback_required=True,
        **metadata,
    )


def _http_error(status_code: int, raw: bytes, *, is_write: bool) -> dict[str, Any]:
    safe_error_code = _safe_http_error_code(raw)
    metadata = {
        "http_status": int(status_code),
        "http_error_code": safe_error_code or None,
        "error_body_byte_size": min(len(raw), MAX_RESPONSE_BYTES),
        "error_body_sha256": hashlib.sha256(raw[:MAX_RESPONSE_BYTES]).hexdigest(),
        "error_body_truncated": len(raw) > MAX_RESPONSE_BYTES,
    }
    # A Store handler may persist a failure/diagnostic state and then return a
    # 4xx response.  Once an apply request has been dispatched, HTTP status
    # alone cannot prove that no mutation happened, so every write rejection
    # must stay in reconciliation until an exact readback closes the outcome.
    if is_write:
        return _uncertain_after_dispatch("store_owner_outcome_uncertain", **metadata)
    return _error(
        "store_owner_http_rejected",
        status="conflict" if status_code == 409 else "blocked",
        request_dispatched=True,
        outcome_uncertain=False,
        **metadata,
    )


def _safe_http_error_code(raw: bytes) -> str:
    if len(raw) > MAX_RESPONSE_BYTES:
        return ""
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError, TypeError):
        return ""
    candidates: list[Any] = []
    if isinstance(payload, dict):
        candidates.extend([payload.get("code"), payload.get("error_code")])
        error = payload.get("error")
        if isinstance(error, dict):
            candidates.extend([error.get("code"), error.get("error_code")])
        detail = payload.get("detail")
        if isinstance(detail, str):
            candidates.append(detail)
        elif isinstance(detail, dict):
            candidates.extend([detail.get("code"), detail.get("error_code")])
    for candidate in candidates:
        normalized = str(candidate or "").strip()
        lowered = normalized.casefold()
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9_.:-]{0,119}", normalized) and lowered.startswith(
            ("store_owner_", "owner_", "idempotency_", "expected_revision_")
        ):
            return normalized
    return ""


def _error(code: str, *, status: str = "failed", **metadata: Any) -> dict[str, Any]:
    safe_metadata = {key: value for key, value in metadata.items() if value is not None}
    return {
        "ok": False,
        "format": STORE_OWNER_FORMAT,
        "status": status,
        "error": {"code": str(code or "store_owner_api_failed")},
        "summary": {"error_code": str(code or "store_owner_api_failed")},
        "meta": safe_metadata,
        "data_included": False,
    }


__all__ = ["STORE_OWNER_FORMAT", "OwnerCapability", "StoreOwnerApiClient"]
