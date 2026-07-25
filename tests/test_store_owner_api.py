from __future__ import annotations

import base64
import io
import json
from datetime import UTC, datetime, timedelta
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

import pytest

from autostop_manager import store_owner_api
from autostop_manager.store_owner_api import OwnerCapability, StoreOwnerApiClient


def test_owner_preflight_contract_matches_current_store_api() -> None:
    assert store_owner_api.OWNER_PREFLIGHT_CONTRACT_VERSION == "store-owner-preflight-v2"


class _Response:
    def __init__(self, payload, *, content_type="application/json", status=200, headers=None):
        self.payload = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.headers = {"Content-Type": content_type, **(headers or {})}
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, limit):
        return self.payload[:limit]


class _Opener:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def _client_with_capability(capability: OwnerCapability) -> StoreOwnerApiClient:
    client = StoreOwnerApiClient(
        agent_api_url="http://127.0.0.1:8010/internal/agent/v1",
        owner_token="owner-secret",
    )
    client._capabilities = {capability.operation_id: capability}
    client._cached_at = store_owner_api.time.monotonic()
    return client


def _json_write_capability(
    *,
    operation_id="update_part",
    method="PATCH",
    path_template="/api/v1/parts/{id}",
    path_parameters=("id",),
    risk="write",
):
    return OwnerCapability(
        operation_id=operation_id,
        method=method,
        path_template=path_template,
        risk=risk,
        request_content_types=("application/json",),
        request_required=True,
        path_parameters=path_parameters,
        schema_hash="a" * 64,
        request_schemas=(
            (
                "application/json",
                {
                    "type": "object",
                    "required": ["name"],
                    "properties": {"name": {"type": "string", "minLength": 1}},
                    "additionalProperties": False,
                },
            ),
        ),
    )


def _revision_response(
    capability: OwnerCapability,
    *,
    concrete_path: str,
    current_revision: str | None,
) -> _Response:
    required = store_owner_api._expected_revision_required(capability)
    return _Response(
        {
            "method": capability.method,
            "path": concrete_path,
            "routeKey": f"{capability.method} {capability.path_template}",
            "currentRevision": current_revision if required else None,
            "revisionKind": "route_opaque" if required else "revision_exempt",
            "expectedRevisionRequired": required,
            "contractVersion": store_owner_api.OWNER_PREFLIGHT_CONTRACT_VERSION,
        }
    )


def _preflight_response(
    *,
    expected_revision: str | None,
    proof: str = "b" * 64,
    expires_delta: timedelta = timedelta(minutes=5),
) -> _Response:
    payload = {
        "ok": True,
        "mode": "dry_run",
        "dryRunProof": proof,
        "receiptId": "receipt-owner-preflight-001",
        "expiresAt": (datetime.now(UTC) + expires_delta).isoformat(),
        "currentRevision": expected_revision,
        "revisionKind": "route_opaque" if expected_revision is not None else "revision_exempt",
        "contractVersion": store_owner_api.OWNER_PREFLIGHT_CONTRACT_VERSION,
    }
    return _Response(payload, headers={store_owner_api.DRY_RUN_PROOF_HEADER: proof})


def _planned_json_write(
    client,
    capability,
    monkeypatch,
    *,
    expected_revision="2026-07-21T00:00:00Z",
    dry_run_key="owner-plan-request-001",
    correlation_id="owner-correlation-001",
):
    path_parameters = {capability.path_parameters[0]: "part-1"} if capability.path_parameters else {}
    concrete_path = capability.path_template.format(**path_parameters)
    responses = []
    if store_owner_api._expected_revision_required(capability):
        responses.append(
            _revision_response(
                capability,
                concrete_path=concrete_path,
                current_revision=expected_revision,
            )
        )
    responses.append(_preflight_response(expected_revision=expected_revision))
    opener = _Opener(responses)
    monkeypatch.setattr(store_owner_api, "build_opener", lambda *_args: opener)
    planned = client.invoke(
        operation_id=capability.operation_id,
        mode="dry_run",
        path_parameters=path_parameters,
        body={"name": "Updated"},
        owner_intent="Plan exact reviewed owner write",
        idempotency_key=dry_run_key,
        correlation_id=correlation_id,
        expected_revision=expected_revision,
    )
    return path_parameters, planned


def test_openapi_inventory_keeps_employee_routes_and_excludes_session_boundaries():
    paths = {}
    for index in range(110):
        paths[f"/api/v1/resources/{index}"] = {"get": {"operationId": f"read_resource_{index}"}}
    paths.update(
        {
            "/api/v1/admin/auth/login": {"post": {"operationId": "login_admin"}},
            "/api/v1/admin/auth/logout": {"post": {"operationId": "logout_admin"}},
            "/api/v1/public/search": {"get": {"operationId": "public_search"}},
            "/api/v1/resources/{resource_id}": {
                "patch": {
                    "operationId": "update_resource",
                    "parameters": [{"in": "query", "name": "notify", "required": True, "schema": {"type": "boolean"}}],
                    "requestBody": {
                        "required": True,
                        "content": {"application/json": {"schema": {"type": "object"}}},
                    },
                }
            },
        }
    )

    capabilities = store_owner_api._parse_capabilities({"paths": paths})

    assert len(capabilities) == 111
    assert "login_admin" not in capabilities
    assert "logout_admin" not in capabilities
    assert "public_search" not in capabilities
    assert capabilities["update_resource"].path_parameters == ("resource_id",)
    assert capabilities["update_resource"].query_parameters == ("notify",)
    assert capabilities["update_resource"].required_query_parameters == ("notify",)
    assert len(capabilities["update_resource"].schema_hash) == 64


def test_risk_classifier_fails_closed_for_unreviewed_collection_and_side_effect_posts():
    assert store_owner_api._risk("POST", "/api/v1/warehouse/suppliers") == "write"
    assert store_owner_api._risk("POST", "/api/v1/categories") == "write"
    assert store_owner_api._risk("POST", "/api/v1/warehouse/receipts/batch") == "high_risk_write"
    assert store_owner_api._risk("POST", "/api/v1/customers/blocked-buyers") == "high_risk_write"
    assert store_owner_api._risk("POST", "/api/v1/marketplaces/exports") == "high_risk_write"
    assert store_owner_api._risk("POST", "/api/v1/future-unreviewed-collection") == "high_risk_write"
    assert store_owner_api._risk("POST", "/api/v1/categories/{id}:deactivate") == "high_risk_write"


def test_owner_transport_origin_is_fail_closed():
    assert store_owner_api._store_origin("http://autostop-app:8000/internal/agent/v1") == "http://autostop-app:8000"
    assert store_owner_api._store_origin("http://127.0.0.1:8010/internal/agent/v1") == "http://127.0.0.1:8010"
    assert store_owner_api._store_origin("http://public.example/internal/agent/v1") == ""
    assert store_owner_api._store_origin("http://user@autostop-app:8000/internal/agent/v1") == ""


def test_high_risk_write_requires_matching_dry_run_and_sends_owner_metadata(monkeypatch):
    capability = OwnerCapability(
        operation_id="delete_part",
        method="DELETE",
        path_template="/api/v1/parts/{id}",
        risk="high_risk_write",
        request_content_types=(),
        request_required=False,
        path_parameters=("id",),
    )
    client = _client_with_capability(capability)
    expected_revision = "2026-07-21T00:00:00Z"
    dry_run_opener = _Opener(
        [
            _revision_response(
                capability,
                concrete_path="/api/v1/parts/part-1",
                current_revision=expected_revision,
            ),
            _preflight_response(expected_revision=expected_revision),
        ]
    )
    monkeypatch.setattr(store_owner_api, "build_opener", lambda *_args: dry_run_opener)
    planned = client.invoke(
        operation_id="delete_part",
        mode="dry_run",
        path_parameters={"id": "part-1"},
        owner_intent="plan exact part deletion",
        idempotency_key="owner-delete-plan-001",
        correlation_id="owner-delete-part-001",
        expected_revision=expected_revision,
    )
    assert planned["ok"] is True
    proof = planned["summary"]["dry_run_proof"]

    blocked = client.invoke(
        operation_id="delete_part",
        mode="apply",
        path_parameters={"id": "part-1"},
        owner_intent="remove exact test part",
        idempotency_key="owner-delete-part-001",
        correlation_id="owner-delete-part-001",
        expected_revision=expected_revision,
    )
    assert blocked["error"]["code"] == "store_owner_dry_run_proof_required"

    opener = _Opener([_Response(b"", status=204)])
    monkeypatch.setattr(store_owner_api, "build_opener", lambda *_args: opener)
    applied = client.invoke(
        operation_id="delete_part",
        mode="apply",
        path_parameters={"id": "part-1"},
        owner_intent="remove exact test part",
        idempotency_key="owner-delete-part-001",
        correlation_id="owner-delete-part-001",
        expected_revision=expected_revision,
        dry_run_proof=proof,
    )

    assert applied["ok"] is True
    assert applied["status"] == "compensating"
    request = opener.requests[0][0]
    headers = dict(request.header_items())
    assert headers["Idempotency-key"] == "owner-delete-part-001"
    assert headers["X-correlation-id"] == "owner-delete-part-001"
    assert headers["X-autostop-action-mode"] == "apply"
    assert headers["X-autostop-dry-run-proof"] == proof
    assert headers["X-autostop-owner-intent"] == "remove exact test part"
    assert "owner-secret" not in json.dumps(applied)


def test_read_dispatch_returns_exact_json_without_write_metadata(monkeypatch):
    capability = OwnerCapability(
        operation_id="get_customer",
        method="GET",
        path_template="/api/v1/customers/{customer_id}",
        risk="read",
        request_content_types=(),
        request_required=False,
        path_parameters=("customer_id",),
    )
    client = _client_with_capability(capability)
    opener = _Opener([_Response({"id": "customer-1", "name": "Test"})])
    monkeypatch.setattr(store_owner_api, "build_opener", lambda *_args: opener)

    result = client.invoke(
        operation_id="get_customer",
        mode="read",
        path_parameters={"customer_id": "customer-1"},
    )

    assert result["ok"] is True
    assert result["data"]["id"] == "customer-1"
    assert opener.requests[0][0].method == "GET"
    assert "Idempotency-key" not in dict(opener.requests[0][0].header_items())


def test_multipart_plan_contains_only_file_metadata_and_hash(monkeypatch):
    capability = OwnerCapability(
        operation_id="upload_photo",
        method="POST",
        path_template="/api/v1/parts/{id}/photos",
        risk="write",
        request_content_types=("multipart/form-data",),
        request_required=True,
        path_parameters=("id",),
        request_schemas=(
            (
                "multipart/form-data",
                {
                    "type": "object",
                    "required": ["file"],
                    "properties": {"file": {"type": "string", "format": "binary"}},
                    "additionalProperties": False,
                },
            ),
        ),
    )
    client = _client_with_capability(capability)
    raw = b"jpeg-test-content"
    expected_revision = "2026-07-21T00:00:00Z"
    opener = _Opener(
        [
            _revision_response(
                capability,
                concrete_path="/api/v1/parts/part-1/photos",
                current_revision=expected_revision,
            ),
            _preflight_response(expected_revision=expected_revision),
        ]
    )
    monkeypatch.setattr(store_owner_api, "build_opener", lambda *_args: opener)

    result = client.invoke(
        operation_id="upload_photo",
        mode="dry_run",
        path_parameters={"id": "part-1"},
        files=[
            {
                "field": "file",
                "filename": "photo.jpg",
                "content_type": "image/jpeg",
                "content_base64": base64.b64encode(raw).decode(),
            }
        ],
        owner_intent="plan exact photo upload",
        idempotency_key="owner-photo-plan-001",
        correlation_id="owner-photo-correlation-001",
        expected_revision=expected_revision,
    )

    assert result["ok"] is True
    metadata = result["summary"]["planned_files"][0]
    assert metadata["byte_size"] == len(raw)
    assert metadata["sha256"] == store_owner_api.hashlib.sha256(raw).hexdigest()
    assert "content_base64" not in json.dumps(result)
    assert opener.requests[1][0].method == "POST"
    assert dict(opener.requests[1][0].header_items())["X-autostop-action-mode"] == "dry_run"


def test_multipart_plan_proof_is_stable_across_random_boundaries(monkeypatch):
    capability = OwnerCapability(
        operation_id="upload_photo",
        method="POST",
        path_template="/api/v1/parts/{id}/photos",
        risk="write",
        request_content_types=("multipart/form-data",),
        request_required=True,
        path_parameters=("id",),
        schema_hash="schema-v1",
        request_schemas=(
            (
                "multipart/form-data",
                {
                    "type": "object",
                    "required": ["file"],
                    "properties": {"file": {"type": "string", "format": "binary"}},
                    "additionalProperties": False,
                },
            ),
        ),
    )
    client = _client_with_capability(capability)
    encoded = base64.b64encode(b"photo-bytes").decode()
    metadata = {
        "owner_intent": "upload exact reviewed photo",
        "correlation_id": "owner-upload-photo-001",
        "expected_revision": "2026-07-21T00:00:00Z",
    }
    expected_revision = metadata["expected_revision"]
    dry_run_opener = _Opener(
        [
            _revision_response(
                capability,
                concrete_path="/api/v1/parts/part-1/photos",
                current_revision=expected_revision,
            ),
            _preflight_response(expected_revision=expected_revision),
        ]
    )
    monkeypatch.setattr(store_owner_api, "build_opener", lambda *_args: dry_run_opener)
    planned = client.invoke(
        operation_id="upload_photo",
        mode="dry_run",
        path_parameters={"id": "part-1"},
        files=[{"field": "file", "filename": "photo.jpg", "content_base64": encoded}],
        idempotency_key="owner-upload-photo-plan-001",
        **metadata,
    )
    opener = _Opener([_Response({"id": "photo-1"}, status=201)])
    monkeypatch.setattr(store_owner_api, "build_opener", lambda *_args: opener)

    applied = client.invoke(
        operation_id="upload_photo",
        mode="apply",
        path_parameters={"id": "part-1"},
        files=[{"field": "file", "filename": "photo.jpg", "content_base64": encoded}],
        dry_run_proof=planned["summary"]["dry_run_proof"],
        idempotency_key="owner-upload-photo-apply-001",
        **metadata,
    )

    assert applied["ok"] is True
    assert applied["status"] == "compensating"


def test_multipart_request_bytes_are_stable_for_owner_idempotency_replay():
    form_one = {"second": {"value": 2}, "first": "one"}
    form_two = {"first": "one", "second": {"value": 2}}
    files = [
        {
            "field": "files",
            "filename": "photo.jpg",
            "content_type": "image/jpeg",
            "content_base64": base64.b64encode(b"stable-photo-bytes").decode(),
        }
    ]

    first = store_owner_api._multipart_payload(form=form_one, files=files)
    second = store_owner_api._multipart_payload(form=form_two, files=files)

    assert first[0] == second[0]
    assert first[1] == second[1]
    assert first[3] == second[3]


def test_regular_write_also_requires_dry_run_proof():
    capability = OwnerCapability(
        operation_id="update_part",
        method="PATCH",
        path_template="/api/v1/parts/{id}",
        risk="write",
        request_content_types=("application/json",),
        request_required=True,
        path_parameters=("id",),
        request_schemas=(
            (
                "application/json",
                {
                    "type": "object",
                    "required": ["name"],
                    "properties": {"name": {"type": "string", "minLength": 1}},
                    "additionalProperties": False,
                },
            ),
        ),
    )
    client = _client_with_capability(capability)

    result = client.invoke(
        operation_id="update_part",
        mode="apply",
        path_parameters={"id": "part-1"},
        body={"name": "Updated"},
        owner_intent="update exact part",
        idempotency_key="owner-update-part-001",
        correlation_id="owner-update-part-001",
        expected_revision="2026-07-21T00:00:00Z",
    )

    assert result["error"]["code"] == "store_owner_dry_run_proof_required"


def test_nested_query_objects_are_rejected():
    capability = OwnerCapability(
        operation_id="list_parts",
        method="GET",
        path_template="/api/v1/parts",
        risk="read",
        request_content_types=(),
        request_required=False,
        path_parameters=(),
        query_parameters=("filter",),
    )
    client = _client_with_capability(capability)

    result = client.invoke(operation_id="list_parts", mode="read", query={"filter": {"name": "x"}})

    assert result["error"]["code"] == "store_owner_query_invalid"


def test_query_names_and_required_parameters_are_enforced():
    capability = OwnerCapability(
        operation_id="list_parts",
        method="GET",
        path_template="/api/v1/parts",
        risk="read",
        request_content_types=(),
        request_required=False,
        path_parameters=(),
        query_parameters=("limit", "status"),
        required_query_parameters=("status",),
    )
    client = _client_with_capability(capability)

    missing = client.invoke(operation_id="list_parts", mode="read", query={"limit": 10})
    unknown = client.invoke(operation_id="list_parts", mode="read", query={"status": "ACTIVE", "raw": True})

    assert missing["error"]["code"] == "store_owner_query_parameters_mismatch"
    assert unknown["error"]["code"] == "store_owner_query_parameters_mismatch"


def test_capability_inventory_can_return_all_current_employee_operations():
    client = StoreOwnerApiClient(
        agent_api_url="http://127.0.0.1:8010/internal/agent/v1",
        owner_token="owner-secret",
    )
    client._capabilities = {
        f"read_resource_{index}": OwnerCapability(
            operation_id=f"read_resource_{index}",
            method="GET",
            path_template=f"/api/v1/resources/{index}",
            risk="read",
            request_content_types=(),
            request_required=False,
            path_parameters=(),
        )
        for index in range(115)
    }
    client._cached_at = store_owner_api.time.monotonic()

    result = client.list_capabilities()

    assert result["summary"]["matches"] == 115
    assert result["summary"]["returned"] == 115
    assert len(result["items"]) == 115


def test_large_binary_response_requires_explicit_opt_in(monkeypatch):
    capability = OwnerCapability(
        operation_id="download_photo",
        method="GET",
        path_template="/api/v1/photos/{id}",
        risk="read",
        request_content_types=(),
        request_required=False,
        path_parameters=("id",),
        response_content_types=("image/jpeg",),
    )
    client = _client_with_capability(capability)
    monkeypatch.setattr(store_owner_api, "MAX_RESPONSE_BYTES", 8)
    monkeypatch.setattr(store_owner_api, "MAX_BINARY_RESPONSE_BYTES", 32)
    opener = _Opener(
        [
            _Response(b"x" * 16, content_type="image/jpeg"),
            _Response(b"x" * 16, content_type="image/jpeg"),
        ]
    )
    monkeypatch.setattr(store_owner_api, "build_opener", lambda *_args: opener)

    blocked = client.invoke(
        operation_id="download_photo",
        mode="read",
        path_parameters={"id": "photo-1"},
    )
    allowed = client.invoke(
        operation_id="download_photo",
        mode="read",
        path_parameters={"id": "photo-1"},
        allow_binary_response=True,
    )

    assert blocked["error"]["code"] == "store_owner_response_too_large"
    assert base64.b64decode(allowed["data"]["content_base64"]) == b"x" * 16


def test_transport_itself_blocks_revision_bypass_but_keeps_reviewed_create_revisionless(monkeypatch):
    update = _json_write_capability(risk="high_risk_write")
    update_client = _client_with_capability(update)
    blocked = update_client.invoke(
        operation_id="update_part",
        mode="dry_run",
        path_parameters={"id": "part-1"},
        body={"name": "Updated"},
    )
    assert blocked["error"]["code"] == "store_owner_expected_revision_required"

    create = _json_write_capability(
        operation_id="create_category",
        method="POST",
        path_template="/api/v1/categories",
        path_parameters=(),
    )
    create_client = _client_with_capability(create)
    opener = _Opener([_preflight_response(expected_revision=None)])
    monkeypatch.setattr(store_owner_api, "build_opener", lambda *_args: opener)
    planned = create_client.invoke(
        operation_id="create_category",
        mode="dry_run",
        body={"name": "Reviewed category"},
        owner_intent="plan exact category create",
        idempotency_key="owner-category-plan-001",
        correlation_id="owner-category-correlation-001",
    )
    assert planned["ok"] is True
    assert planned["summary"]["revision_required"] is False


def test_revision_discovery_and_aggregate_dry_run_dispatch_real_server_requests(monkeypatch):
    capability = _json_write_capability(
        operation_id="update_settings",
        path_template="/api/v1/settings",
        path_parameters=(),
        risk="high_risk_write",
    )
    client = _client_with_capability(capability)
    expected_revision = "f" * 64
    revision_response = _revision_response(
        capability,
        concrete_path="/api/v1/settings",
        current_revision=expected_revision,
    )
    revision_opener = _Opener([revision_response])
    monkeypatch.setattr(store_owner_api, "build_opener", lambda *_args: revision_opener)

    discovered = client.invoke(
        operation_id="update_settings",
        mode="revision",
        body={"name": "Updated"},
    )

    assert discovered["ok"] is True
    assert discovered["summary"]["current_revision"] == expected_revision
    revision_request = revision_opener.requests[0][0]
    assert revision_request.method == "GET"
    revision_query = parse_qs(urlsplit(revision_request.full_url).query)
    assert revision_query == {"method": ["PATCH"], "path": ["/api/v1/settings"]}

    dry_run_opener = _Opener(
        [
            revision_response,
            _preflight_response(expected_revision=expected_revision, proof="7" * 64),
        ]
    )
    monkeypatch.setattr(store_owner_api, "build_opener", lambda *_args: dry_run_opener)
    planned = client.invoke(
        operation_id="update_settings",
        mode="dry_run",
        body={"name": "Updated"},
        owner_intent="Plan exact settings update",
        idempotency_key="owner-settings-dry-001",
        correlation_id="owner-settings-flow-001",
        expected_revision=expected_revision,
    )

    assert planned["status"] == "planned"
    assert planned["summary"]["dry_run_proof"] == "7" * 64
    assert [request.method for request, _timeout in dry_run_opener.requests] == ["GET", "PATCH"]
    write_headers = dict(dry_run_opener.requests[1][0].header_items())
    assert write_headers["X-autostop-action-mode"] == "dry_run"
    assert "X-autostop-dry-run-proof" not in write_headers


@pytest.mark.parametrize(
    "expires_delta",
    [timedelta(seconds=-1), timedelta(minutes=10)],
    ids=["already-expired", "beyond-server-ttl"],
)
def test_dry_run_rejects_unbounded_server_receipt_expiry(monkeypatch, expires_delta):
    capability = _json_write_capability(
        operation_id="create_category",
        method="POST",
        path_template="/api/v1/categories",
        path_parameters=(),
    )
    client = _client_with_capability(capability)
    opener = _Opener(
        [
            _preflight_response(
                expected_revision=None,
                expires_delta=expires_delta,
            )
        ]
    )
    monkeypatch.setattr(store_owner_api, "build_opener", lambda *_args: opener)

    result = client.invoke(
        operation_id="create_category",
        mode="dry_run",
        body={"name": "Reviewed category"},
        owner_intent="Plan exact category create",
        idempotency_key="owner-category-expiry-001",
        correlation_id="owner-category-expiry-001",
    )

    assert result["error"]["code"] == "store_owner_dry_run_response_invalid"


def test_openapi_schema_validates_body_path_and_query_before_dry_run():
    capability = _json_write_capability()
    capability = OwnerCapability(
        **{
            **capability.__dict__,
            "path_parameter_schemas": (("id", {"type": "string", "pattern": "^part-[0-9]+$"}),),
            "query_parameters": ("notify",),
            "required_query_parameters": ("notify",),
            "query_parameter_schemas": (("notify", {"type": "boolean"}),),
        }
    )
    client = _client_with_capability(capability)

    bad_body = client.invoke(
        operation_id="update_part",
        mode="dry_run",
        path_parameters={"id": "part-1"},
        query={"notify": True},
        body={"name": "", "unexpected": True},
        expected_revision="rev-1",
    )
    bad_path = client.invoke(
        operation_id="update_part",
        mode="dry_run",
        path_parameters={"id": "../part-1"},
        query={"notify": True},
        body={"name": "Updated"},
        expected_revision="rev-1",
    )
    bad_query = client.invoke(
        operation_id="update_part",
        mode="dry_run",
        path_parameters={"id": "part-1"},
        query={"notify": "yes"},
        body={"name": "Updated"},
        expected_revision="rev-1",
    )

    assert bad_body["error"]["code"] == "store_owner_request_schema_invalid"
    assert bad_path["error"]["code"] == "store_owner_path_parameter_schema_invalid"
    assert bad_query["error"]["code"] == "store_owner_query_schema_invalid"


def test_path_traversal_and_oversized_query_are_rejected_before_dispatch():
    path_capability = OwnerCapability(
        operation_id="get_part",
        method="GET",
        path_template="/api/v1/parts/{id}",
        risk="read",
        request_content_types=(),
        request_required=False,
        path_parameters=("id",),
    )
    path_client = _client_with_capability(path_capability)
    traversal = path_client.invoke(
        operation_id="get_part",
        mode="read",
        path_parameters={"id": ".."},
    )
    assert traversal["error"]["code"] == "store_owner_path_parameter_invalid"

    query_capability = OwnerCapability(
        operation_id="list_parts",
        method="GET",
        path_template="/api/v1/parts",
        risk="read",
        request_content_types=(),
        request_required=False,
        path_parameters=(),
        query_parameters=("query",),
    )
    query_client = _client_with_capability(query_capability)
    oversized = query_client.invoke(
        operation_id="list_parts",
        mode="read",
        query={"query": "x" * (store_owner_api.MAX_QUERY_BYTES + 1)},
    )
    assert oversized["error"]["code"] == "store_owner_query_too_large"


@pytest.mark.parametrize(
    "failure_kind",
    ["client_400_after_commit", "server_503", "oversized_json", "invalid_json"],
)
def test_write_post_dispatch_failures_are_always_uncertain(monkeypatch, failure_kind):
    capability = _json_write_capability()
    client = _client_with_capability(capability)
    path_parameters, planned = _planned_json_write(client, capability, monkeypatch)
    if failure_kind == "client_400_after_commit":
        response = HTTPError(
            "http://127.0.0.1/api/v1/parts/part-1",
            400,
            "external order was not confirmed",
            {},
            io.BytesIO(json.dumps({"detail": "external_order_not_confirmed"}).encode()),
        )
    elif failure_kind == "server_503":
        response = HTTPError(
            "http://127.0.0.1/api/v1/parts/part-1",
            503,
            "unavailable",
            {},
            io.BytesIO(json.dumps({"detail": "owner_receipt_uncertain", "customer": "secret"}).encode()),
        )
    elif failure_kind == "oversized_json":
        monkeypatch.setattr(store_owner_api, "MAX_RESPONSE_BYTES", 8)
        response = _Response({"name": "response larger than bound"})
    else:
        response = _Response(b"{invalid-json", content_type="application/json")
    monkeypatch.setattr(store_owner_api, "build_opener", lambda *_args: _Opener([response]))

    result = client.invoke(
        operation_id=capability.operation_id,
        mode="apply",
        path_parameters=path_parameters,
        body={"name": "Updated"},
        owner_intent="Update exact reviewed part",
        idempotency_key="owner-update-uncertain-001",
        correlation_id="owner-update-uncertain-001",
        expected_revision="2026-07-21T00:00:00Z",
        dry_run_proof=planned["summary"]["dry_run_proof"],
    )

    assert result["status"] == "compensating"
    assert result["meta"]["outcome_uncertain"] is True
    assert result["meta"]["readback_required"] is True
    assert "secret" not in json.dumps(result)


def test_http_validation_error_after_apply_is_uncertain_and_error_body_is_never_returned(monkeypatch):
    capability = _json_write_capability()
    client = _client_with_capability(capability)
    path_parameters, planned = _planned_json_write(client, capability, monkeypatch)
    response = HTTPError(
        "http://127.0.0.1/api/v1/parts/part-1",
        422,
        "invalid",
        {},
        io.BytesIO(json.dumps({"detail": "WBA12345678901234", "customer": "Private Name"}).encode()),
    )
    monkeypatch.setattr(store_owner_api, "build_opener", lambda *_args: _Opener([response]))

    result = client.invoke(
        operation_id=capability.operation_id,
        mode="apply",
        path_parameters=path_parameters,
        body={"name": "Updated"},
        owner_intent="Update exact reviewed part",
        idempotency_key="owner-update-rejected-001",
        correlation_id="owner-update-rejected-001",
        expected_revision="2026-07-21T00:00:00Z",
        dry_run_proof=planned["summary"]["dry_run_proof"],
    )

    assert result["status"] == "compensating"
    assert result["meta"]["outcome_uncertain"] is True
    assert result["meta"]["readback_required"] is True
    assert result["meta"].get("http_error_code") is None
    assert "Private" not in json.dumps(result)
    assert "WBA12345678901234" not in json.dumps(result)


@pytest.mark.parametrize(
    "detail",
    ["owner_idempotency_uncertain", "expected_revision_conflict"],
)
def test_apply_conflict_errors_remain_uncertain_after_dispatch(monkeypatch, detail):
    capability = _json_write_capability()
    client = _client_with_capability(capability)
    path_parameters, planned = _planned_json_write(client, capability, monkeypatch)
    response = HTTPError(
        "http://127.0.0.1/api/v1/parts/part-1",
        409,
        "conflict",
        {},
        io.BytesIO(json.dumps({"detail": detail}).encode()),
    )
    monkeypatch.setattr(store_owner_api, "build_opener", lambda *_args: _Opener([response]))

    result = client.invoke(
        operation_id=capability.operation_id,
        mode="apply",
        path_parameters=path_parameters,
        body={"name": "Updated"},
        owner_intent="Update exact reviewed part",
        idempotency_key="owner-update-conflict-001",
        correlation_id="owner-update-conflict-001",
        expected_revision="2026-07-21T00:00:00Z",
        dry_run_proof=planned["summary"]["dry_run_proof"],
    )

    assert result["status"] == "compensating"
    assert result["meta"]["outcome_uncertain"] is True
    assert result["meta"]["readback_required"] is True


def test_idempotency_replay_is_reported_only_from_transport_header(monkeypatch):
    capability = _json_write_capability()
    client = _client_with_capability(capability)
    path_parameters, planned = _planned_json_write(client, capability, monkeypatch)
    opener = _Opener(
        [
            _Response(
                {"id": "part-1", "idempotency_replay": False},
                headers={"X-Autostop-Idempotency-Replay": "true"},
            )
        ]
    )
    monkeypatch.setattr(store_owner_api, "build_opener", lambda *_args: opener)

    result = client.invoke(
        operation_id=capability.operation_id,
        mode="apply",
        path_parameters=path_parameters,
        body={"name": "Updated"},
        owner_intent="Update exact reviewed part",
        idempotency_key="owner-update-replay-001",
        correlation_id="owner-update-replay-001",
        expected_revision="2026-07-21T00:00:00Z",
        dry_run_proof=planned["summary"]["dry_run_proof"],
    )

    assert result["meta"]["idempotency_replay"] is True
    assert result["status"] == "compensating"


def test_invalid_success_response_never_becomes_completed(monkeypatch):
    capability = OwnerCapability(
        operation_id="get_part",
        method="GET",
        path_template="/api/v1/parts/{id}",
        risk="read",
        request_content_types=(),
        request_required=False,
        path_parameters=("id",),
        response_content_types=("application/json",),
        response_statuses=("200",),
        response_content_contracts=(("200", "application/json"),),
        response_schemas=(
            (
                "200",
                "application/json",
                {
                    "type": "object",
                    "required": ["id"],
                    "properties": {"id": {"type": "string"}},
                    "additionalProperties": False,
                },
            ),
        ),
        response_contract_enforced=True,
    )
    client = _client_with_capability(capability)
    monkeypatch.setattr(
        store_owner_api,
        "build_opener",
        lambda *_args: _Opener([_Response({"unexpected": "payload"})]),
    )

    result = client.invoke(operation_id="get_part", mode="read", path_parameters={"id": "part-1"})

    assert result["ok"] is False
    assert result["status"] == "failed"
    assert result["error"]["code"] == "store_owner_response_schema_invalid"


def test_empty_success_body_is_rejected_when_openapi_declares_json_schema(monkeypatch):
    capability = OwnerCapability(
        operation_id="get_part",
        method="GET",
        path_template="/api/v1/parts/{id}",
        risk="read",
        request_content_types=(),
        request_required=False,
        path_parameters=("id",),
        response_content_types=("application/json",),
        response_statuses=("200",),
        response_content_contracts=(("200", "application/json"),),
        response_schemas=(("200", "application/json", {"type": "object"}),),
        response_contract_enforced=True,
    )
    client = _client_with_capability(capability)
    monkeypatch.setattr(
        store_owner_api,
        "build_opener",
        lambda *_args: _Opener([_Response(b"")]),
    )

    result = client.invoke(operation_id="get_part", mode="read", path_parameters={"id": "part-1"})

    assert result["ok"] is False
    assert result["status"] == "failed"
    assert result["error"]["code"] == "store_owner_response_schema_invalid"


def test_rossko_credentials_never_egress_from_owner_get_or_patch(monkeypatch):
    read_capability = OwnerCapability(
        operation_id="get_rossko_settings",
        method="GET",
        path_template="/api/v1/warehouse/rossko-settings",
        risk="read",
        request_content_types=(),
        request_required=False,
        path_parameters=(),
    )
    read_client = _client_with_capability(read_capability)
    secrets = {"key1": "rossko-secret-one", "key2": "rossko-secret-two"}
    monkeypatch.setattr(
        store_owner_api,
        "build_opener",
        lambda *_args: _Opener([_Response({**secrets, "markupPercent": 10})]),
    )

    read_result = read_client.invoke(operation_id="get_rossko_settings", mode="read")

    assert read_result["error"]["code"] == "store_owner_sensitive_response_blocked"
    assert not read_result["data_included"]
    assert all(secret not in json.dumps(read_result) for secret in secrets.values())

    patch_capability = OwnerCapability(
        operation_id="update_rossko_settings",
        method="PATCH",
        path_template="/api/v1/warehouse/rossko-settings",
        risk="write",
        request_content_types=("application/json",),
        request_required=True,
        path_parameters=(),
        request_schemas=(("application/json", {"type": "object"}),),
    )
    patch_client = _client_with_capability(patch_capability)
    revision = "2026-07-21T00:00:00Z"
    opener = _Opener(
        [
            _revision_response(
                patch_capability,
                concrete_path=patch_capability.path_template,
                current_revision=revision,
            ),
            _preflight_response(expected_revision=revision),
        ]
    )
    monkeypatch.setattr(store_owner_api, "build_opener", lambda *_args: opener)
    planned = patch_client.invoke(
        operation_id="update_rossko_settings",
        mode="dry_run",
        body={**secrets, "markupPercent": 10},
        owner_intent="Update exact reviewed Rossko settings",
        idempotency_key="owner-rossko-dry-001",
        correlation_id="owner-rossko-flow-001",
        expected_revision=revision,
    )
    assert planned["ok"] is True
    monkeypatch.setattr(
        store_owner_api,
        "build_opener",
        lambda *_args: _Opener([_Response({**secrets, "markupPercent": 10})]),
    )

    patch_result = patch_client.invoke(
        operation_id="update_rossko_settings",
        mode="apply",
        body={**secrets, "markupPercent": 10},
        owner_intent="Update exact reviewed Rossko settings",
        idempotency_key="owner-rossko-apply-001",
        correlation_id="owner-rossko-flow-001",
        expected_revision=revision,
        dry_run_proof=planned["summary"]["dry_run_proof"],
    )

    assert patch_result["status"] == "compensating"
    assert patch_result["error"]["code"] == "store_owner_sensitive_response_blocked"
    assert not patch_result["data_included"]
    assert all(secret not in json.dumps(patch_result) for secret in secrets.values())


def test_masked_rossko_response_supports_apply_and_exact_safe_readback(monkeypatch):
    safe_response = {
        "key1": None,
        "key2": None,
        "key1Configured": True,
        "key2Configured": True,
        "settingsRevision": "e" * 64,
        "markupPercent": 10,
    }
    read_capability = OwnerCapability(
        operation_id="get_rossko_settings",
        method="GET",
        path_template="/api/v1/warehouse/rossko-settings",
        risk="read",
        request_content_types=(),
        request_required=False,
        path_parameters=(),
    )
    read_client = _client_with_capability(read_capability)
    monkeypatch.setattr(
        store_owner_api,
        "build_opener",
        lambda *_args: _Opener([_Response(safe_response)]),
    )
    reread = read_client.invoke(operation_id="get_rossko_settings", mode="read")
    assert reread["ok"] is True
    assert reread["data"] == safe_response

    patch_capability = OwnerCapability(
        operation_id="update_rossko_settings",
        method="PATCH",
        path_template="/api/v1/warehouse/rossko-settings",
        risk="write",
        request_content_types=("application/json",),
        request_required=True,
        path_parameters=(),
        request_schemas=(("application/json", {"type": "object"}),),
    )
    patch_client = _client_with_capability(patch_capability)
    revision = "2026-07-21T00:00:00Z"
    secrets = {"key1": "rossko-secret-one", "key2": "rossko-secret-two"}
    opener = _Opener(
        [
            _revision_response(
                patch_capability,
                concrete_path=patch_capability.path_template,
                current_revision=revision,
            ),
            _preflight_response(expected_revision=revision),
        ]
    )
    monkeypatch.setattr(store_owner_api, "build_opener", lambda *_args: opener)
    planned = patch_client.invoke(
        operation_id="update_rossko_settings",
        mode="dry_run",
        body={**secrets, "markupPercent": 10},
        owner_intent="Update exact reviewed Rossko settings",
        idempotency_key="owner-rossko-safe-dry-001",
        correlation_id="owner-rossko-safe-flow-001",
        expected_revision=revision,
    )
    monkeypatch.setattr(
        store_owner_api,
        "build_opener",
        lambda *_args: _Opener([_Response(safe_response)]),
    )
    applied = patch_client.invoke(
        operation_id="update_rossko_settings",
        mode="apply",
        body={**secrets, "markupPercent": 10},
        owner_intent="Update exact reviewed Rossko settings",
        idempotency_key="owner-rossko-safe-apply-001",
        correlation_id="owner-rossko-safe-flow-001",
        expected_revision=revision,
        dry_run_proof=planned["summary"]["dry_run_proof"],
    )

    assert applied["status"] == "compensating"
    assert applied["data"] == safe_response
    assert all(secret not in json.dumps(applied) for secret in secrets.values())


@pytest.mark.parametrize(
    "secret_key",
    [
        "accessToken",
        "refreshToken",
        "clientSecret",
        "privateKey",
        "secretKey",
        "clientAPIKey",
    ],
)
def test_sensitive_response_filter_is_camel_case_aware(secret_key):
    assert store_owner_api._contains_sensitive_response_data({secret_key: "must-not-egress"})
    assert not store_owner_api._contains_sensitive_response_data(
        {
            "key1Configured": True,
            "passwordResetAt": "2026-07-21T00:00:00Z",
            "secretConfigured": True,
            "settingsRevision": "a" * 64,
            "tokenCount": 2,
        }
    )


def test_apply_blocks_when_refreshed_openapi_changes_prepared_plan(monkeypatch):
    capability_a = _json_write_capability()
    capability_b = OwnerCapability(
        **{
            **capability_a.__dict__,
            "schema_hash": "b" * 64,
        }
    )
    client = _client_with_capability(capability_a)
    client._loaded_from_openapi = True
    prepared = client.prepare_invocation(
        operation_id=capability_a.operation_id,
        path_parameters={"id": "part-1"},
        body={"name": "Updated"},
        expected_revision="revision-1",
    )
    monkeypatch.setattr(
        client,
        "_load_capabilities",
        lambda *, force=False: {capability_b.operation_id: capability_b if force else capability_a},
    )

    result = client.invoke(
        operation_id=capability_a.operation_id,
        mode="apply",
        path_parameters={"id": "part-1"},
        body={"name": "Updated"},
        owner_intent="Apply exact reviewed plan",
        idempotency_key="owner-plan-race-apply-001",
        correlation_id="owner-plan-race-flow-001",
        expected_revision="revision-1",
        dry_run_proof="c" * 64,
        expected_plan_hash=prepared["summary"]["plan_hash"],
    )

    assert result["status"] == "conflict"
    assert result["error"]["code"] == "store_owner_plan_changed"
    assert result["meta"].get("request_dispatched") is None


def test_embedded_binary_and_write_binary_opt_in_are_blocked(monkeypatch):
    read_capability = OwnerCapability(
        operation_id="get_document",
        method="GET",
        path_template="/api/v1/documents/{id}",
        risk="read",
        request_content_types=(),
        request_required=False,
        path_parameters=("id",),
    )
    read_client = _client_with_capability(read_capability)
    monkeypatch.setattr(
        store_owner_api,
        "build_opener",
        lambda *_args: _Opener([_Response({"content_base64": base64.b64encode(b"secret").decode()})]),
    )
    embedded = read_client.invoke(
        operation_id="get_document",
        mode="read",
        path_parameters={"id": "document-1"},
    )
    assert embedded["error"]["code"] == "store_owner_binary_response_not_allowed"
    assert "c2VjcmV0" not in json.dumps(embedded)

    write_capability = _json_write_capability()
    write_client = _client_with_capability(write_capability)
    blocked_write = write_client.invoke(
        operation_id="update_part",
        mode="dry_run",
        path_parameters={"id": "part-1"},
        body={"name": "Updated"},
        expected_revision="rev-1",
        allow_binary_response=True,
    )
    assert blocked_write["error"]["code"] == "store_owner_binary_response_not_allowed"


def test_json_and_nested_form_serialization_are_byte_stable():
    capability = OwnerCapability(
        operation_id="update_part",
        method="PATCH",
        path_template="/api/v1/parts/{id}",
        risk="write",
        request_content_types=("application/json",),
        request_required=True,
        path_parameters=("id",),
        request_schemas=(
            (
                "application/json",
                {
                    "type": "object",
                    "required": ["first", "second"],
                    "properties": {"first": {}, "second": {}},
                    "additionalProperties": False,
                },
            ),
        ),
    )
    first = store_owner_api._request_payload(
        capability=capability,
        body={"second": {"b": 2, "a": 1}, "first": "one"},
        form={},
        files=[],
    )
    second = store_owner_api._request_payload(
        capability=capability,
        body={"first": "one", "second": {"a": 1, "b": 2}},
        form={},
        files=[],
    )
    assert first["data"] == second["data"]
    assert first["canonical_sha256"] == second["canonical_sha256"]

    multipart_one = store_owner_api._multipart_payload(form={"data": {"b": 2, "a": 1}}, files=[])
    multipart_two = store_owner_api._multipart_payload(form={"data": {"a": 1, "b": 2}}, files=[])
    assert multipart_one[0] == multipart_two[0]


def test_multipart_headers_and_aggregate_bounds_fail_closed(monkeypatch):
    encoded = base64.b64encode(b"1234").decode()
    with pytest.raises(ValueError, match="store_owner_multipart_header_invalid"):
        store_owner_api._multipart_payload(
            form={},
            files=[{"field": "file\r\nX-Test", "filename": "photo.jpg", "content_base64": encoded}],
        )

    monkeypatch.setattr(store_owner_api, "MAX_FILES_BYTES", 4)
    with pytest.raises(ValueError, match="store_owner_files_too_large"):
        store_owner_api._multipart_payload(
            form={},
            files=[
                {"field": "file", "filename": "one.jpg", "content_base64": encoded},
                {"field": "file", "filename": "two.jpg", "content_base64": encoded},
            ],
        )

    monkeypatch.setattr(store_owner_api, "MAX_REQUEST_BYTES", 4)
    with pytest.raises(ValueError, match="store_owner_request_too_large"):
        store_owner_api._multipart_payload(form={"field": "long-value"}, files=[])


def test_owner_intent_unicode_is_ascii_safe_and_not_echoed(monkeypatch):
    capability = _json_write_capability()
    client = _client_with_capability(capability)
    path_parameters, planned = _planned_json_write(client, capability, monkeypatch)
    opener = _Opener([_Response({"id": "part-1"})])
    monkeypatch.setattr(store_owner_api, "build_opener", lambda *_args: opener)

    result = client.invoke(
        operation_id=capability.operation_id,
        mode="apply",
        path_parameters=path_parameters,
        body={"name": "Updated"},
        owner_intent="Обновить точную карточку товара",
        idempotency_key="owner-update-unicode-001",
        correlation_id="owner-update-unicode-001",
        expected_revision="2026-07-21T00:00:00Z",
        dry_run_proof=planned["summary"]["dry_run_proof"],
    )

    header = dict(opener.requests[0][0].header_items())["X-autostop-owner-intent"]
    assert header.startswith("utf8-b64:")
    assert header.isascii()
    assert "Обновить" not in json.dumps(result, ensure_ascii=False)
