"""Verify Store writes through action contracts and the real HTTP adapter."""

from __future__ import annotations

import asyncio
from copy import deepcopy
import hashlib
from io import BytesIO
import json
import os
import socket
import urllib.request
from urllib.parse import parse_qs, urlsplit

import pytest

from autostop_manager import store_api
from autostop_manager.storage import StoreState
from autostop_manager.store_api import StoreApiClient
from autostop_manager.store_integration import StoreIntegration


BEFORE = "2026-10-16T10:00:00+00:00"
AFTER = "2026-10-16T10:01:00+00:00"
ACTIONS = {
    "batch": ("store_batch", "set_batch_storage_location", "storage_location", "storage_location"),
    "assignment": ("store_quote_request", "assign_quote_request", "assignee_id", "assigned_user_id"),
    "status": ("store_quote_request", "set_quote_request_status", "status", "status"),
    "comment": ("store_quote_request", "update_quote_request_comment", "internal_comment", "internal_comment_sha256"),
}
STRING_ACTIONS = ["batch", "assignment"]
MISSING = object()
NONSTRING_VALUES = [
    pytest.param(None, id="null"),
    pytest.param(True, id="true"),
    pytest.param(False, id="false"),
    pytest.param(123, id="integer"),
    pytest.param(0, id="zero"),
    pytest.param(1.5, id="float"),
    pytest.param([], id="empty-list"),
    pytest.param([1, "x"], id="list"),
    pytest.param({}, id="empty-object"),
    pytest.param({"slot": 1}, id="object"),
]


@pytest.fixture(autouse=True)
def isolated_store(monkeypatch, tmp_path):
    def denied(*_args, **_kwargs):
        raise AssertionError("readback test attempted real network or private acquisition")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)
    monkeypatch.setattr(urllib.request, "urlopen", denied)
    monkeypatch.setattr(store_api, "urlopen", denied)
    monkeypatch.setenv("AUTOSTOP_MANAGER_ENV_FILE", os.devnull)
    monkeypatch.setenv("AUTOSTOP_MANAGER_DB", str(tmp_path / "manager.sqlite3"))
    monkeypatch.setenv("AUTOSTOP_J1_CACHE_DIR", str(tmp_path / "j1"))
    monkeypatch.setenv("AUTOSTOP_J1_VIN_CACHE_DIR", str(tmp_path / "vin"))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))


def envelope(*, item=None, meta=None):
    return {
        "ok": True,
        "format": "store_agent_v1",
        "status": "completed",
        "summary": {},
        "items": [] if item is None else [item],
        "changes": [],
        "page": {},
        "warnings": [],
        "meta": {} if meta is None else meta,
    }


class WireCase:
    def __init__(self, monkeypatch, tmp_path, action, planned, observed, *, uncertain=False):
        self.domain, self.action, self.field, self.readback_field = ACTIONS[action]
        self.target = "batch-16" if action == "batch" else "quote-16"
        self.arguments = {
            "domain": self.domain,
            "action": self.action,
            "target_id": self.target,
            "planned_changes": {self.field: planned},
            "owner_intent": "Synthetic owner instruction for the exact test entity",
            "expected_updated_at": BEFORE,
            "idempotency_key": "synthetic-readback-16",
            "correlation_id": "contract:synthetic-readback-16",
            "mode": "apply",
        }
        self.before = {"entity": self.domain, "id": self.target, "updated_at": BEFORE}
        self.after = {"entity": self.domain, "id": self.target, "updated_at": AFTER}
        if observed is not MISSING:
            self.after[self.readback_field] = deepcopy(observed)
        self.post = envelope()
        self.uncertain = uncertain
        self.calls = []
        self.store = StoreState(tmp_path / "state.sqlite3")
        self.client = StoreApiClient(
            api_url="https://store.synthetic.example/internal/agent/v1",
            read_token="synthetic-read-token",
            manage_token="synthetic-manage-token",
            quote_token="synthetic-quote-token",
            max_read_attempts=1,
        )
        self.integration = StoreIntegration(client=self.client, store=self.store)
        monkeypatch.setattr(store_api, "urlopen", self.urlopen)

    def urlopen(self, request, timeout):
        parsed = urlsplit(request.full_url)
        assert parsed.scheme == "https" and parsed.netloc == "store.synthetic.example"
        assert timeout > 0
        method = request.get_method()
        body = json.loads(request.data) if request.data is not None else None
        self.calls.append((method, parsed.path, body))
        index = len(self.calls)
        if index in (1, 3):
            assert method == "GET"
            assert parsed.path == f"/internal/agent/v1/entities/{self.domain}/{self.target}"
            assert parse_qs(parsed.query) == {"detail": ["summary"]}
            assert request.get_header("Authorization") == "Bearer synthetic-read-token"
            payload = envelope(item=self.before if index == 1 else self.after)
        else:
            assert index == 2, "no automatic retry or second POST is allowed"
            assert method == "POST"
            assert parsed.path == f"/internal/agent/v1/actions/{self.action}"
            assert not parsed.query
            assert request.get_header("Authorization") == "Bearer synthetic-manage-token"
            if self.uncertain:
                raise TimeoutError("synthetic response lost after dispatch")
            payload = self.post
        return BytesIO(json.dumps(payload, ensure_ascii=False).encode("utf-8"))

    def call(self, tool=None, *, encoded=False):
        snapshot = deepcopy((self.arguments, self.before, self.after, self.post))
        if tool is None:
            response = self.integration.management_action(**self.arguments)
        else:
            arguments = deepcopy(self.arguments)
            if encoded:
                arguments["planned_changes"] = json.dumps(arguments["planned_changes"])
            wire_snapshot = deepcopy(arguments)
            content, response = asyncio.run(tool.run(arguments, convert_result=True))
            assert json.loads(content[0].text) == response
            assert arguments == wire_snapshot
        assert (self.arguments, self.before, self.after, self.post) == snapshot
        return response

    def assert_transaction(self, *, post_read=True):
        assert [method for method, _, _ in self.calls] == (["GET", "POST", "GET"] if post_read else ["GET", "POST"])
        sent = self.calls[1][2]
        assert sent["target_id"] == self.target
        assert sent["expected_updated_at"] == self.arguments["expected_updated_at"]
        assert sent["idempotency_key"] == self.arguments["idempotency_key"]
        assert sent["correlation_id"] == self.arguments["correlation_id"]
        assert sent["owner_intent"] == self.arguments["owner_intent"]
        assert sent["mode"] == self.arguments["mode"]


@pytest.fixture
def wire_case(monkeypatch, tmp_path):
    def create(action, planned, observed, **kwargs):
        return WireCase(monkeypatch, tmp_path, action, planned, observed, **kwargs)

    return create


def assert_unverified(response, code="store_apply_readback_mismatch", *, fields=None):
    assert response["ok"] is False
    assert response["format"] == "store_agent_v1"
    assert response["status"] == "compensating"
    assert response["summary"]["error_code"] == code
    assert response["summary"]["write_applied_unverified"] is True
    assert response["meta"]["pre_state_read"] is True
    assert response["meta"]["post_state_read"] is True
    assert response["meta"]["readback_verified"] is False
    if fields is not None:
        assert response["summary"]["failed_fields"] == fields


def assert_verified(response):
    assert response["ok"] is True and response["status"] == "completed"
    assert response["meta"]["readback_verified"] is True
    assert response["meta"]["pre_state_read"] is True
    assert response["meta"]["post_state_read"] is True


@pytest.mark.parametrize("action", STRING_ACTIONS)
@pytest.mark.parametrize("observed", NONSTRING_VALUES)
def test_matching_python_representation_of_json_value_is_not_string_readback(wire_case, action, observed):
    case = wire_case(action, str(observed), observed)
    result = case.call()
    case.assert_transaction()
    assert case.calls[1][2]["planned_changes"] == {case.field: str(observed)}
    assert_unverified(result, fields=[case.field])


@pytest.mark.parametrize("action", STRING_ACTIONS)
@pytest.mark.parametrize("value", ["None", "True", "False", "123", "0", "1.5", "[]", "[1, 'x']", "{}", "{'slot': 1}"])
def test_literal_strings_remain_valid_even_when_they_look_like_other_types(wire_case, action, value):
    case = wire_case(action, value, value)
    assert_verified(case.call())
    case.assert_transaction()


@pytest.mark.parametrize("action", STRING_ACTIONS)
@pytest.mark.parametrize(
    "planned,observed",
    [("  B-2  ", "B-2"), ("employee-7", "\t employee-7 \n"), ("\u2003Стеллаж 7\u00a0", "\u00a0Стеллаж 7\u2003")],
)
def test_real_strings_keep_canonical_strip_and_unicode_content(wire_case, action, planned, observed):
    case = wire_case(action, planned, observed)
    assert_verified(case.call())
    case.assert_transaction()
    assert case.calls[1][2]["planned_changes"] == {case.field: planned.strip()}


@pytest.mark.parametrize("action", STRING_ACTIONS)
@pytest.mark.parametrize("observed", [MISSING, "different", "b-2"], ids=["missing", "different", "case-sensitive"])
def test_absent_or_different_string_keeps_exact_failed_field(wire_case, action, observed):
    case = wire_case(action, "B-2", observed)
    assert_unverified(case.call(), fields=[case.field])
    case.assert_transaction()


@pytest.mark.parametrize("action", STRING_ACTIONS)
@pytest.mark.parametrize("observed", [None, True, []], ids=["null", "bool", "container"])
def test_uncertain_apply_exposes_typed_mismatch_without_reposting(wire_case, action, observed):
    case = wire_case(action, str(observed), observed, uncertain=True)
    result = case.call()
    case.assert_transaction()
    assert_unverified(result, "store_apply_outcome_uncertain", fields=[case.field])
    assert result["summary"]["transport_error_code"] == "store_timeout_or_network_error"
    assert result["summary"]["readback_target_matches"] is True
    assert result["meta"]["request_dispatched"] is True
    assert result["meta"]["outcome_uncertain"] is True
    assert result["meta"]["attempt_count"] == 1


@pytest.mark.parametrize("action", STRING_ACTIONS)
def test_uncertain_apply_stays_uncertain_even_with_matching_string(wire_case, action):
    case = wire_case(action, "None", "None", uncertain=True)
    assert_unverified(case.call(), "store_apply_outcome_uncertain", fields=[])
    case.assert_transaction()


@pytest.mark.parametrize("action", STRING_ACTIONS)
@pytest.mark.parametrize("observed", ["None", None], ids=["actual-string", "wrong-type"])
def test_idempotency_replay_does_not_bypass_readback_type_verification(wire_case, action, observed):
    case = wire_case(action, "None", observed)
    case.before["updated_at"] = AFTER
    case.post["meta"]["idempotency_replay"] = True
    result = case.call()
    case.assert_transaction()
    assert result["meta"]["pre_read_revision_mismatch"] is True
    if isinstance(observed, str):
        assert_verified(result)
        assert result["meta"]["idempotency_replay"] is True
    else:
        assert_unverified(result, fields=[case.field])


@pytest.mark.parametrize("action", STRING_ACTIONS)
@pytest.mark.parametrize("version", [None, BEFORE, "2026-10-16T09:59:00+00:00"], ids=["missing", "unchanged", "older"])
def test_matching_string_still_requires_advanced_readback_revision(wire_case, action, version):
    case = wire_case(action, "123", "123")
    if version is None:
        del case.after["updated_at"]
    else:
        case.after["updated_at"] = version
    assert_unverified(case.call(), "store_apply_readback_version_not_advanced")
    case.assert_transaction()


@pytest.mark.parametrize("action", STRING_ACTIONS)
@pytest.mark.parametrize("failure", ["target", "stale-dry-run"])
def test_preflight_failure_does_not_dispatch_a_write(wire_case, action, failure):
    case = wire_case(action, "123", "123")
    if failure == "target":
        case.before["id"] = "other-entity"
        code, status = "store_preflight_target_mismatch", "blocked"
    else:
        case.arguments["mode"] = "dry_run"
        case.before["updated_at"] = AFTER
        code, status = "store_expected_updated_at_mismatch", "conflict"
    result = case.call()
    assert result["ok"] is False and result["status"] == status
    assert result["summary"]["error_code"] == code
    assert [method for method, _, _ in case.calls] == ["GET"]


@pytest.mark.parametrize("action", STRING_ACTIONS)
def test_same_value_on_another_entity_never_verifies_target(wire_case, action):
    case = wire_case(action, "True", "True")
    case.after["id"] = "another-target"
    assert_unverified(case.call(), "store_apply_readback_target_mismatch")
    case.assert_transaction()


@pytest.mark.parametrize("action", STRING_ACTIONS)
def test_dry_run_does_not_claim_post_state_verification(wire_case, action):
    case = wire_case(action, "None", None)
    case.arguments["mode"] = "dry_run"
    case.post["status"] = "dry_run"
    result = case.call()
    assert result["ok"] is True and result["status"] == "dry_run"
    assert result["meta"]["post_state_read"] is False
    assert result["meta"]["readback_verified"] is False
    case.assert_transaction(post_read=False)


@pytest.mark.parametrize("observed", ["WAITING_FOR_APPROVAL", " waiting_for_approval "])
def test_status_canonicalization_is_preserved_through_http_projection(wire_case, observed):
    case = wire_case("status", " waiting_for_approval ", observed)
    assert_verified(case.call())
    case.assert_transaction()
    assert case.calls[1][2]["planned_changes"] == {"status": "WAITING_FOR_APPROVAL"}


@pytest.mark.parametrize(
    "comment,canonical", [(" Проверить VIN ", "comment:Проверить VIN"), (None, "none:"), ("  ", "none:")]
)
def test_internal_comment_uses_canonical_hash_not_plaintext_readback(wire_case, comment, canonical):
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    case = wire_case("comment", comment, digest)
    assert_verified(case.call())
    case.assert_transaction()
    assert "internal_comment" not in case.after


@pytest.fixture
def native_tool(monkeypatch):
    from mcp.server.fastmcp import FastMCP

    from autostop_manager import mcp_telemetry, mcp_tools

    class OtherAdapter:
        def __init__(self, *_args, **_kwargs):
            pass

    for name in ("StoreOwnerApiClient", "StoreQuoteConductor", "StoreQuoteOwnerApi"):
        monkeypatch.setattr(mcp_tools, name, OtherAdapter)
    for name in (
        "get_store_api_url",
        "get_store_read_token",
        "get_store_manage_token",
        "get_store_quote_token",
        "get_store_owner_token",
    ):
        monkeypatch.setattr(mcp_tools, name, lambda: "")
    monkeypatch.setattr(mcp_telemetry.LOGGER, "disabled", True)

    def register(case):
        server = FastMCP("synthetic-store-readback")
        mcp_tools.register_manager_tools(
            server, case.store, store_client=case.client, include_tools={"store_management_action"}
        )
        return server._tool_manager.get_tool("store_management_action")

    return register


@pytest.mark.parametrize("encoded", [False, True], ids=["raw-object", "json-encoded-object"])
@pytest.mark.parametrize(
    "action,planned,observed",
    [("batch", "None", None), ("assignment", "123", 123), ("batch", "[]", "[]"), ("assignment", "True", "True")],
)
def test_registered_tool_keeps_string_readback_contract_and_result_channel_parity(
    wire_case, native_tool, encoded, action, planned, observed
):
    case = wire_case(action, planned, observed)
    result = case.call(native_tool(case), encoded=encoded)
    case.assert_transaction()
    if isinstance(observed, str):
        assert_verified(result)
    else:
        assert_unverified(result, fields=[case.field])


@pytest.mark.parametrize("encoded", [False, True], ids=["raw-object", "json-encoded-object"])
@pytest.mark.parametrize("action", STRING_ACTIONS)
def test_registered_tool_keeps_uncertain_mismatch_and_does_not_repost(wire_case, native_tool, encoded, action):
    case = wire_case(action, "None", None, uncertain=True)
    result = case.call(native_tool(case), encoded=encoded)
    case.assert_transaction()
    assert_unverified(result, "store_apply_outcome_uncertain", fields=[case.field])
