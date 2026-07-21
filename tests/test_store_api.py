from __future__ import annotations

import io
import json
import threading
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse

import pytest

import autostop_manager.store_api as store_api_module
from autostop_manager.store_api import MAX_STORE_LIMIT, StoreApiClient


class _Response:
    def __init__(self, payload: dict | bytes, *, headers: dict[str, str] | None = None):
        self.body = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
        self.headers = headers or {"Content-Type": "application/json"}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, size: int = -1) -> bytes:
        return self.body if size < 0 else self.body[:size]


def _envelope(
    *,
    ok: bool = True,
    status: str = "completed",
    items: list[dict] | None = None,
    changes: list[dict] | None = None,
    has_more: bool = False,
    next_cursor: str | None = "cursor-next",
    summary: dict | None = None,
) -> dict:
    return {
        "ok": ok,
        "format": "store_agent_v1",
        "status": status,
        "summary": summary or {},
        "items": items or [],
        "changes": changes or [],
        "page": {"has_more": has_more, "next_cursor": next_cursor, "limit": 25},
        "warnings": [],
        "meta": {"snapshot_at": "2026-07-16T10:00:00+07:00"},
    }


def _client(**kwargs) -> StoreApiClient:
    quote_token = kwargs.pop("quote_token", "")
    return StoreApiClient(
        api_url="http://store.internal/internal/agent/v1",
        read_token="read-secret",
        manage_token="manage-secret",
        quote_token=quote_token,
        circuit_cooldown_seconds=600,
        **kwargs,
    )


def test_client_repr_and_local_status_never_expose_tokens():
    client = _client()

    rendered = repr(client)
    status = client.runtime_status(live=False)

    assert "read-secret" not in rendered
    assert "manage-secret" not in rendered
    assert status["summary"]["read_configured"] is True
    assert status["summary"]["manage_configured"] is True
    assert "secret" not in str(status)


def test_live_runtime_status_keeps_redacted_adapter_readiness(monkeypatch):
    monkeypatch.setattr(store_api_module, "urlopen", lambda *_args, **_kwargs: _Response(_envelope()))

    result = _client().runtime_status(live=True)

    assert result["ok"] is True
    assert result["summary"]["adapter"] == {
        "read_configured": True,
        "manage_configured": True,
        "quote_configured": False,
        "circuit_open": False,
        "consecutive_failures": 0,
    }
    assert result["meta"]["live_check"] is True
    assert "read-secret" not in str(result)
    assert "manage-secret" not in str(result)


def test_live_runtime_status_accepts_complete_change_feed_contract(monkeypatch):
    payload = _envelope(
        summary={
            "features": {
                "quote_full_read_enabled": True,
                "quote_draft_write_enabled": True,
                "supplier_lookup_enabled": True,
                "rossko_configured": True,
            },
            "change_feed": {
                "generation": "generation-1",
                "current_sequence": 42,
                "min_available_position": 0,
                "event_count": 42,
                "oldest_changed_at": "2026-07-21T00:00:00+00:00",
                "newest_changed_at": "2026-07-21T01:00:00+00:00",
                "retention_mode": "append_only_no_gc",
                "payload_contract": "pii_free_entity_refs",
                "tracked_table_count": 32,
                "privacy_or_infrastructure_exempt_table_count": 11,
                "entity_type_count": 33,
            },
        }
    )
    monkeypatch.setattr(store_api_module, "urlopen", lambda *_args, **_kwargs: _Response(payload))

    result = _client().runtime_status(live=True)

    assert result["ok"] is True
    assert result["summary"]["features"]["quote_full_read_enabled"] is True
    assert result["summary"]["features"]["quote_draft_write_enabled"] is True
    assert result["summary"]["features"]["supplier_lookup_enabled"] is True
    assert result["summary"]["change_feed"]["payload_contract"] == "pii_free_entity_refs"
    assert result["summary"]["change_feed"]["tracked_table_count"] == 32


def test_digest_retries_safe_get_and_sends_bounded_query(monkeypatch):
    calls = []

    def fake_urlopen(request, timeout):
        calls.append((request, timeout))
        if len(calls) == 1:
            raise URLError("temporary")
        return _Response(_envelope(items=[{"entity": "store_order", "id": "123"}], has_more=True))

    monkeypatch.setattr(store_api_module, "urlopen", fake_urlopen)
    result = _client(timeout=2.5, max_read_attempts=2).digest(since="2026-07-15T00:00:00+07:00", limit=999)

    assert result["ok"] is True
    assert result["meta"]["attempt_count"] == 2
    parsed = urlparse(calls[-1][0].full_url)
    query = parse_qs(parsed.query)
    assert parsed.path == "/internal/agent/v1/digest"
    assert query["limit"] == [str(MAX_STORE_LIMIT)]
    assert query["since"] == ["2026-07-15T00:00:00+07:00"]
    assert calls[-1][0].get_header("Authorization") == "Bearer read-secret"


def test_bootstrap_snapshot_is_one_get_without_cursor_ack_or_private_error_text(monkeypatch):
    calls = []
    payload = _envelope(
        next_cursor=None,
        summary={
            "store_api_ready": True,
            "product_count": 42,
            "active_order_count": 3,
            "open_quote_request_count": 2,
            "inventory": {
                "position_count": 40,
                "physical_qty": 100,
                "reserved_qty": 7,
                "available_qty": 93,
                "purchase_stock_value": "1000.00",
                "retail_stock_value": "1500.00",
                "low_stock_threshold": 1,
                "low_stock_count": 4,
            },
            "marketplaces": {
                "accounts_total": 2,
                "active_accounts": 2,
                "listing_status": {"published": 10, "failed": 1},
                "export_job_status": {"sent": 20, "failed": 2},
                "export_errors": {
                    "counts": {"last_24_hours": 1, "last_7_days": 2, "all_time": 2},
                    "latest": [
                        {
                            "error_at": "2026-07-19T10:00:00+00:00",
                            "error_code": "MARKETPLACE_EXPORT_FAILED",
                            "part_id": "part-1",
                            "account_id": "account-1",
                            "attempt_count": 3,
                        }
                    ],
                },
            },
            "contract_version": "store_agent_v1",
            "bootstrap_snapshot_version": 1,
        },
    )

    def fake_urlopen(request, timeout):
        calls.append((request, timeout))
        return _Response(payload)

    monkeypatch.setattr(store_api_module, "urlopen", fake_urlopen)

    result = _client(max_read_attempts=1).bootstrap_snapshot()

    assert result["ok"] is True
    assert result["summary"]["product_count"] == 42
    assert len(calls) == 1
    parsed = urlparse(calls[0][0].full_url)
    assert parsed.path == "/internal/agent/v1/bootstrap-snapshot"
    assert parsed.query == ""
    assert result["page"]["next_cursor"] is None
    assert "ack" not in json.dumps(result)


def test_bootstrap_snapshot_rejects_raw_marketplace_error_fields(monkeypatch):
    payload = _envelope(
        next_cursor=None,
        summary={
            "store_api_ready": True,
            "marketplaces": {
                "export_errors": {
                    "counts": {"last_24_hours": 1, "last_7_days": 1, "all_time": 1},
                    "latest": [
                        {
                            "error_at": "2026-07-19T10:00:00+00:00",
                            "error_code": "MARKETPLACE_EXPORT_FAILED",
                            "part_id": "part-1",
                            "account_id": "account-1",
                            "attempt_count": 1,
                            "message": "Bearer must-not-pass",
                        }
                    ],
                }
            },
        },
    )
    monkeypatch.setattr(store_api_module, "urlopen", lambda *_args, **_kwargs: _Response(payload))

    result = _client(max_read_attempts=1).bootstrap_snapshot()

    assert result["ok"] is False
    assert result["summary"]["error_code"] == "store_response_schema_invalid"
    assert "must-not-pass" not in json.dumps(result)


def test_management_post_has_exact_body_uses_manage_identity_and_is_not_retried(monkeypatch):
    calls = []

    def fake_urlopen(request, timeout):
        calls.append((request, timeout))
        return _Response(_envelope(summary={"target_id": "quote-1"}))

    monkeypatch.setattr(store_api_module, "urlopen", fake_urlopen)
    result = _client(max_read_attempts=3).management_action(
        operation="assign_quote_request",
        target_id="quote-1",
        expected_updated_at="2026-07-16T10:00:00+07:00",
        owner_intent="Назначь заявку quote-1 сотруднику employee-7",
        idempotency_key="quote-1-assign-v1",
        correlation_id="contract:12345678",
        mode="dry_run",
        planned_changes={"assignee_id": "employee-7"},
    )

    assert result["ok"] is True
    assert result["meta"]["request_dispatched"] is True
    assert result["meta"]["outcome_uncertain"] is False
    assert len(calls) == 1
    request = calls[0][0]
    assert request.method == "POST"
    assert request.full_url.endswith("/actions/assign_quote_request")
    assert request.get_header("Authorization") == "Bearer manage-secret"
    assert json.loads(request.data) == {
        "target_id": "quote-1",
        "expected_updated_at": "2026-07-16T10:00:00+07:00",
        "owner_intent": "Назначь заявку quote-1 сотруднику employee-7",
        "idempotency_key": "quote-1-assign-v1",
        "correlation_id": "contract:12345678",
        "mode": "dry_run",
        "planned_changes": {"assignee_id": "employee-7"},
    }


def test_valid_409_envelope_is_preserved_and_does_not_trip_circuit(monkeypatch):
    conflict = _envelope(
        ok=False,
        status="conflict",
        summary={
            "error_code": "store_expected_updated_at_mismatch",
            "message": "revision conflict",
            "details": {"current_updated_at": "2026-07-16T10:01:00+00:00"},
        },
        next_cursor=None,
    )

    def fake_urlopen(_request, timeout):
        assert timeout > 0
        raise HTTPError(
            "http://store/actions/mark_order_ready",
            409,
            "conflict",
            hdrs=None,
            fp=io.BytesIO(json.dumps(conflict).encode("utf-8")),
        )

    monkeypatch.setattr(store_api_module, "urlopen", fake_urlopen)
    client = _client(failure_threshold=1)
    result = client.management_action(
        operation="mark_order_ready",
        target_id="order-1",
        expected_updated_at="old-version",
        owner_intent="Переведи точный заказ order-1 в READY",
        idempotency_key="order-1-ready-v1",
        correlation_id="contract:87654321",
        mode="apply",
        planned_changes={"status": "READY"},
    )

    assert result["status"] == "conflict"
    assert result["summary"]["error_code"] == "store_expected_updated_at_mismatch"
    assert result["summary"]["details"]["current_updated_at"].endswith("+00:00")
    assert result["meta"]["http_status"] == 409
    assert result["meta"]["request_dispatched"] is True
    assert result["meta"]["outcome_uncertain"] is False
    assert client.local_status()["circuit_open"] is False
    assert client.local_status()["consecutive_failures"] == 0


def test_repeated_403_without_valid_body_never_opens_circuit(monkeypatch):
    def fake_urlopen(_request, timeout):
        assert timeout > 0
        raise HTTPError("http://store/search", 403, "forbidden", hdrs=None, fp=io.BytesIO(b"not-json"))

    monkeypatch.setattr(store_api_module, "urlopen", fake_urlopen)
    client = _client(failure_threshold=1)

    for _ in range(5):
        result = client.search(entity="store_order", query_text="123")
        assert result["status"] == "blocked"
        assert result["summary"]["error_code"] == "store_http_403"

    assert client.local_status()["circuit_open"] is False
    assert client.local_status()["consecutive_failures"] == 0


def test_transport_failures_open_circuit_and_prevent_further_calls(monkeypatch):
    calls = 0

    def fake_urlopen(_request, timeout):
        assert timeout > 0
        nonlocal calls
        calls += 1
        raise URLError("offline")

    monkeypatch.setattr(store_api_module, "urlopen", fake_urlopen)
    client = _client(max_read_attempts=1, failure_threshold=2)

    assert client.digest()["summary"]["error_code"] == "store_timeout_or_network_error"
    assert client.digest()["summary"]["error_code"] == "store_timeout_or_network_error"
    assert client.digest()["summary"]["error_code"] == "store_circuit_open"
    assert calls == 2


def test_redirect_is_rejected_without_forwarding_bearer_to_target():
    trusted_requests: list[str | None] = []
    attacker_requests: list[str | None] = []

    class AttackerHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            attacker_requests.append(self.headers.get("Authorization"))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(_envelope()).encode("utf-8"))

        def log_message(self, _format, *_args):
            return

    attacker = ThreadingHTTPServer(("127.0.0.1", 0), AttackerHandler)
    attacker_url = f"http://127.0.0.1:{attacker.server_port}/capture"

    class TrustedHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            trusted_requests.append(self.headers.get("Authorization"))
            self.send_response(302)
            self.send_header("Location", attacker_url)
            self.end_headers()

        def log_message(self, _format, *_args):
            return

    trusted = ThreadingHTTPServer(("127.0.0.1", 0), TrustedHandler)
    threads = [
        threading.Thread(target=attacker.serve_forever, daemon=True),
        threading.Thread(target=trusted.serve_forever, daemon=True),
    ]
    for thread in threads:
        thread.start()

    try:
        client = StoreApiClient(
            api_url=f"http://127.0.0.1:{trusted.server_port}/internal/agent/v1",
            read_token="read-secret",
            manage_token="manage-secret",
            max_read_attempts=1,
        )
        result = client.digest()
    finally:
        trusted.shutdown()
        attacker.shutdown()
        trusted.server_close()
        attacker.server_close()
        for thread in threads:
            thread.join(timeout=2)

    assert result["summary"]["error_code"] == "store_redirect_rejected"
    assert trusted_requests == ["Bearer read-secret"]
    assert attacker_requests == []
    assert client.local_status()["consecutive_failures"] == 1


def test_schema_validation_rejects_unknown_sensitive_or_free_text_fields(monkeypatch):
    payload = _envelope(
        items=[
            {
                "entity": "store_order",
                "id": "order-1",
                "customer_email": "client@example.test",
                "customer": {"name": "Private Person"},
                "vin": "WDD00000000000000",
                "access_token": "never-return",
                "backend_token": "also-never-return",
            }
        ]
    )
    monkeypatch.setattr(store_api_module, "urlopen", lambda *_args, **_kwargs: _Response(payload))

    result = _client().search(entity="store_order", query_text="order-1")
    assert result["summary"]["error_code"] == "store_response_schema_invalid"
    assert result["items"] == []

    invalid = _envelope(items=[{"entity": "store_order", "badKey": "value"}])
    monkeypatch.setattr(store_api_module, "urlopen", lambda *_args, **_kwargs: _Response(invalid))
    assert _client().digest()["summary"]["error_code"] == "store_response_schema_invalid"


@pytest.mark.parametrize(
    "invalid",
    [
        _envelope(summary={"quantity": float("nan")}),
        {**_envelope(), "summary": "not-an-object"},
        {**_envelope(), "warnings": ["ok", {"not": "a string"}]},
        {**_envelope(), "items": ["not-an-object"]},
    ],
)
def test_invalid_envelope_shapes_and_nonfinite_numbers_are_rejected(monkeypatch, invalid):
    monkeypatch.setattr(store_api_module, "urlopen", lambda *_args, **_kwargs: _Response(invalid))

    result = _client().digest()

    assert result["summary"]["error_code"] == "store_response_schema_invalid"


def test_deeply_nested_response_is_rejected_as_schema_invalid(monkeypatch):
    nested = b'{"nested":' * 1_100 + b"null" + b"}" * 1_100
    payload = (
        b'{"ok":true,"format":"store_agent_v1","status":"completed","summary":'
        + nested
        + b',"items":[],"changes":[],"page":{},"warnings":[],"meta":{}}'
    )
    monkeypatch.setattr(store_api_module, "urlopen", lambda *_args, **_kwargs: _Response(payload))

    result = _client().digest()

    assert result["summary"]["error_code"] == "store_response_schema_invalid"


def test_overwide_nested_response_is_rejected_as_schema_invalid(monkeypatch):
    payload = _envelope(summary={f"field_{index}": index for index in range(501)})
    monkeypatch.setattr(store_api_module, "urlopen", lambda *_args, **_kwargs: _Response(payload))

    result = _client().digest()

    assert result["summary"]["error_code"] == "store_response_schema_invalid"


@pytest.mark.parametrize("entity", ["orders", "store_client", "", "../store_order"])
def test_entity_allowlist_blocks_unknown_values_before_transport(entity):
    with pytest.raises(ValueError, match="entity"):
        _client().search(entity=entity)


def test_store_contacts_fail_closed_and_contacts_detail_is_not_exposed(monkeypatch):
    payload = _envelope(items=[{"entity": "store_order", "id": "1", "customer_phone": "+79990000000"}])
    monkeypatch.setattr(store_api_module, "urlopen", lambda *_args, **_kwargs: _Response(payload))
    client = _client()

    summary = client.entity_context(entity="store_order", entity_id="1", detail="summary")
    assert summary["summary"]["error_code"] == "store_response_schema_invalid"
    assert summary["items"] == []
    with pytest.raises(ValueError, match="detail"):
        client.entity_context(entity="store_order", entity_id="1", detail="contacts")


def test_exact_quote_full_read_uses_quote_token_and_keeps_authorized_pii(monkeypatch):
    captured = {}
    payload = _envelope(
        summary={"entity": "store_quote_request", "entity_id": "quote-1", "detail": "full"},
        items=[
            {
                "entity": "store_quote_request",
                "id": "quote-1",
                "entity_type": "store_quote_request",
                "entity_id": "quote-1",
                "updated_at": "2026-07-19T10:00:00+00:00",
                "request_number": 2,
                "status": "NEW",
                "assigned_user_id": None,
                "assigned_user_name": None,
                "items_count": 1,
                "has_internal_comment": False,
                "internal_comment_sha256": "a" * 64,
                "created_at": "2026-07-19T09:00:00+00:00",
                "notes_count": 0,
                "agent_draft_count": 0,
                "published_offer_count": 0,
                "content_trust": "untrusted_customer_input",
                "customer_name": "Иван Петров",
                "phone": "+79990000000",
                "email": "client@example.test",
                "telegram_username": "client",
                "vin": "WDD00000000000001",
                "customer_comment": "Нужен фильтр",
                "delivery_method": "PICKUP",
                "delivery_address": None,
                "internal_comment": None,
                "agreement_comment": None,
                "converted_order_id": None,
                "approved_at": None,
                "closed_at": None,
                "items": [
                    {
                        "item_id": "item-1",
                        "part_description": "Фильтр",
                        "quantity": 1,
                        "comment": None,
                        "sort_order": 1,
                        "offers": [],
                    }
                ],
                "notes": [],
                "items_has_more": False,
                "nested_limit": 100,
            }
        ],
    )

    def fake_urlopen(request, **_kwargs):
        captured["authorization"] = request.headers["Authorization"]
        return _Response(payload)

    monkeypatch.setattr(store_api_module, "urlopen", fake_urlopen)
    result = _client(quote_token="quote-secret").entity_context(
        entity="store_quote_request", entity_id="quote-1", detail="full"
    )
    assert result["items"][0]["phone"] == "+79990000000"
    assert result["items"][0]["vin"] == "WDD00000000000001"
    assert captured["authorization"] == "Bearer quote-secret"


def test_exact_quote_full_read_fails_closed_without_quote_token():
    result = _client().entity_context(entity="store_quote_request", entity_id="quote-1", detail="full")
    assert result["summary"]["error_code"] == "store_quote_token_missing"
    assert result["meta"]["request_dispatched"] is False


def test_exact_quote_vin_photo_detail_is_scoped_and_rejects_unknown_photo_fields(monkeypatch):
    captured = {}
    payload = _envelope(
        summary={
            "entity": "store_quote_request",
            "entity_id": "quote-1",
            "detail": "full_with_vin_photo",
        },
        items=[
            {
                "entity": "store_quote_request",
                "id": "quote-1",
                "entity_type": "store_quote_request",
                "entity_id": "quote-1",
                "updated_at": "2026-07-19T10:00:00+00:00",
                "request_number": 2,
                "status": "NEW",
                "assigned_user_id": None,
                "assigned_user_name": None,
                "items_count": 1,
                "has_internal_comment": False,
                "internal_comment_sha256": "a" * 64,
                "created_at": "2026-07-19T09:00:00+00:00",
                "notes_count": 0,
                "agent_draft_count": 0,
                "published_offer_count": 0,
                "content_trust": "untrusted_customer_input",
                "customer_name": "Иван Петров",
                "phone": "+79990000000",
                "email": "client@example.test",
                "telegram_username": "client",
                "vin": None,
                "customer_comment": "Нужен фильтр",
                "delivery_method": "PICKUP",
                "delivery_address": None,
                "internal_comment": None,
                "agreement_comment": None,
                "converted_order_id": None,
                "approved_at": None,
                "closed_at": None,
                "items": [],
                "notes": [],
                "items_has_more": False,
                "nested_limit": 100,
                "vin_photo": {
                    "sha256": "b" * 64,
                    "content_type": "image/jpeg",
                    "byte_size": 12345,
                    "width": 1600,
                    "height": 900,
                },
            }
        ],
    )

    def fake_urlopen(request, **_kwargs):
        captured["authorization"] = request.headers["Authorization"]
        return _Response(payload)

    monkeypatch.setattr(store_api_module, "urlopen", fake_urlopen)
    result = _client(quote_token="quote-secret").entity_context(
        entity="store_quote_request", entity_id="quote-1", detail="full_with_vin_photo"
    )

    assert result["items"][0]["vin_photo"]["sha256"] == "b" * 64
    assert captured["authorization"] == "Bearer quote-secret"

    payload["items"][0]["vin_photo"]["file_name"] = "private.jpg"
    invalid = _client(quote_token="quote-secret").entity_context(
        entity="store_quote_request", entity_id="quote-1", detail="full_with_vin_photo"
    )
    assert invalid["summary"]["error_code"] == "store_response_schema_invalid"


def test_quote_vin_photo_preview_uses_quote_token_and_returns_only_bounded_jpeg(monkeypatch):
    captured = {}
    preview = b"jpeg-preview-bytes"

    def fake_urlopen(request, **_kwargs):
        captured["url"] = request.full_url
        captured["authorization"] = request.headers["Authorization"]
        return _Response(preview, headers={"Content-Type": "image/jpeg"})

    monkeypatch.setattr(store_api_module, "urlopen", fake_urlopen)
    result = _client(quote_token="quote-secret").quote_vin_photo_preview(
        quote_request_id="quote-1", expected_photo_sha256="c" * 64
    )

    assert result["ok"] is True
    assert result["summary"]["attachment"]["sha256"] == "c" * 64
    assert base64.b64decode(result["data"]["content_base64"]) == preview
    assert captured["authorization"] == "Bearer quote-secret"
    parsed = urlparse(captured["url"])
    assert parsed.path.endswith("/entities/store_quote_request/quote-1/vin-photo-preview")
    assert parse_qs(parsed.query)["expected_sha256"] == ["c" * 64]


def test_quote_vin_photo_preview_fails_closed_for_wrong_media_or_missing_quote_token(monkeypatch):
    monkeypatch.setattr(
        store_api_module,
        "urlopen",
        lambda *_args, **_kwargs: _Response(b"not-a-jpeg", headers={"Content-Type": "image/png"}),
    )
    invalid = _client(quote_token="quote-secret").quote_vin_photo_preview(
        quote_request_id="quote-1", expected_photo_sha256="d" * 64
    )
    assert invalid["summary"]["error_code"] == "store_attachment_response_invalid"

    missing = _client().quote_vin_photo_preview(quote_request_id="quote-1", expected_photo_sha256="d" * 64)
    assert missing["summary"]["error_code"] == "store_quote_token_missing"


@pytest.mark.parametrize(
    ("call", "message"),
    [
        (lambda client: client.search(entity="store_part", query_text="x" * 201), "query"),
        (lambda client: client.search(entity="store_part", filters={"brand": "x" * 2001}), "filters"),
        (lambda client: client.search(entity="store_part", filters=["not-an-object"]), "filters"),
        (
            lambda client: client.entity_context(entity="store_order", entity_id="x" * 121),
            "entity_id",
        ),
    ],
)
def test_oversized_or_malformed_search_inputs_are_rejected_before_url_build(call, message):
    with pytest.raises(ValueError, match=message):
        call(_client())


def test_invalid_correlation_id_is_rejected_before_write_transport():
    with pytest.raises(ValueError, match="correlation_id"):
        _client().management_action(
            operation="mark_order_ready",
            target_id="order-1",
            expected_updated_at="version-1",
            owner_intent="Переведи заказ order-1 в READY",
            idempotency_key="order-1-ready-v1",
            correlation_id="short",
            mode="dry_run",
            planned_changes={"status": "READY"},
        )


def test_real_app_digest_status_distribution_list_passes_snake_case_contract(monkeypatch):
    mirrored_changes = [{"entity": "store_order", "id": "order-1", "status": "IN_PROGRESS"}]
    payload = _envelope(
        items=mirrored_changes,
        changes=mirrored_changes,
        summary={
            "snapshot_at": "2026-07-16T10:00:00+00:00",
            "order_status_distribution": [
                {"status": "IN_PROGRESS", "count": 2},
                {"status": "READY", "count": 1},
            ],
            "inventory": {"position_count": 3, "low_stock_items": []},
        },
    )
    monkeypatch.setattr(store_api_module, "urlopen", lambda *_args, **_kwargs: _Response(payload))

    result = _client().digest(limit=25)

    assert result["ok"] is True
    assert result["items"] == mirrored_changes
    assert result["changes"] == mirrored_changes
    assert result["summary"]["order_status_distribution"] == [
        {"status": "IN_PROGRESS", "count": 2},
        {"status": "READY", "count": 1},
    ]


@pytest.mark.parametrize(
    ("operation", "planned_changes", "result_summary", "changes"),
    [
        (
            "assign_quote_request",
            {"assignee_id": "employee-7"},
            {"entity_type": "store_quote_request", "entity_id": "quote-1", "assigned_user_id": "employee-7"},
            [{"field": "assigned_user_id", "before": None, "after": "employee-7"}],
        ),
        (
            "set_quote_request_status",
            {"status": "IN_PROGRESS"},
            {"entity_type": "store_quote_request", "entity_id": "quote-1", "status": "IN_PROGRESS"},
            [{"field": "status", "before": "NEW", "after": "IN_PROGRESS"}],
        ),
        (
            "update_quote_request_comment",
            {"internal_comment": "Проверить VIN"},
            {
                "entity_type": "store_quote_request",
                "entity_id": "quote-1",
                "has_internal_comment": True,
                "internal_comment_sha256": "a" * 64,
            },
            [
                {"field": "has_internal_comment", "before": False, "after": True},
                {"field": "internal_comment_sha256", "before": "b" * 64, "after": "a" * 64},
            ],
        ),
        (
            "set_batch_storage_location",
            {"storage_location": "B-2"},
            {"entity_type": "store_batch", "entity_id": "batch-1", "storage_location": "B-2"},
            [{"field": "storage_location", "before": "A-1", "after": "B-2"}],
        ),
        (
            "mark_order_ready",
            {"status": "READY"},
            {"entity_type": "store_order", "entity_id": "order-1", "status": "READY"},
            [
                {"field": "status", "before": "IN_PROGRESS", "after": "READY"},
                {"field": "ready_at", "before": None, "after": "generated_on_apply"},
            ],
        ),
    ],
)
def test_all_five_real_app_action_envelopes_have_independent_change_limits(
    monkeypatch,
    operation,
    planned_changes,
    result_summary,
    changes,
):
    payload = _envelope(
        status="dry_run",
        summary={
            "operation": operation,
            "mode": "dry_run",
            "target_id": result_summary["entity_id"],
            "changed": True,
            "result": result_summary,
        },
        changes=changes,
        next_cursor=None,
    )
    payload["meta"].update(
        {
            "correlation_id": "store_action_contract_12345678",
            "idempotency_key": f"{operation}-dry-run-v1",
            "idempotency_replay": False,
            "effects": [],
            "external_effect_state": "NOT_APPLICABLE",
        }
    )
    monkeypatch.setattr(store_api_module, "urlopen", lambda *_args, **_kwargs: _Response(payload))

    result = _client().management_action(
        operation=operation,
        target_id=result_summary["entity_id"],
        expected_updated_at="2026-07-16T10:00:00+00:00",
        owner_intent=f"Preview exact {operation}",
        idempotency_key=f"{operation}-dry-run-v1",
        correlation_id="store_action_contract_12345678",
        mode="dry_run",
        planned_changes=planned_changes,
    )

    assert result["ok"] is True
    assert result["items"] == []
    assert result["changes"] == changes


@pytest.mark.parametrize(
    "failure_kind",
    ["network", "http_500", "invalid_json", "oversize", "schema_invalid"],
)
def test_post_unknown_outcomes_are_classified_uncertain_and_never_retried(monkeypatch, failure_kind):
    calls = 0

    def fake_urlopen(_request, timeout):
        nonlocal calls
        calls += 1
        assert timeout > 0
        if failure_kind == "network":
            raise URLError("connection reset")
        if failure_kind == "http_500":
            raise HTTPError(
                "http://store/actions/mark_order_ready",
                500,
                "internal error",
                hdrs=None,
                fp=io.BytesIO(b"server failed"),
            )
        if failure_kind == "invalid_json":
            return _Response(b"not-json")
        if failure_kind == "oversize":
            return _Response(b"x" * 1_025)
        return _Response({**_envelope(), "summary": "not-an-object"})

    monkeypatch.setattr(store_api_module, "urlopen", fake_urlopen)
    result = _client(max_read_attempts=3, max_response_bytes=1_024).management_action(
        operation="mark_order_ready",
        target_id="order-1",
        expected_updated_at="version-1",
        owner_intent="Mark exact order-1 READY",
        idempotency_key="order-1-ready-apply-v1",
        correlation_id="store_action_contract_12345678",
        mode="apply",
        planned_changes={"status": "READY"},
    )

    assert result["ok"] is False
    assert result["meta"]["request_dispatched"] is True
    assert result["meta"]["outcome_uncertain"] is True
    assert calls == 1


def test_post_pre_dispatch_failure_is_explicit_and_not_uncertain():
    client = StoreApiClient(
        api_url="http://store.internal/internal/agent/v1",
        read_token="read-secret",
        manage_token="",
    )

    result = client.management_action(
        operation="mark_order_ready",
        target_id="order-1",
        expected_updated_at="version-1",
        owner_intent="Mark exact order-1 READY",
        idempotency_key="order-1-ready-apply-v1",
        correlation_id="store_action_contract_12345678",
        mode="apply",
        planned_changes={"status": "READY"},
    )

    assert result["summary"]["error_code"] == "store_manage_token_missing"
    assert result["meta"]["request_dispatched"] is False
    assert result["meta"]["outcome_uncertain"] is False
