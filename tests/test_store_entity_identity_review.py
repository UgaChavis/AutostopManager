"""Synthetic wire regressions for exact Store identity; no live Store access.

Exercise the real action contract, HTTP decoder/projection and orchestration.
Only urlopen is replaced. Expected IDs are opaque strings, never UUID-only.
Contradictory nonempty aliases cannot establish one exact target; null/empty
aliases retain the existing fallback compatibility. Each action preserves its exact target, one POST, and closed responses.
"""

from __future__ import annotations

from copy import deepcopy
from io import BytesIO
import json
import os
import socket
import urllib.request
from urllib.parse import parse_qs, quote, urlsplit

import pytest

from autostop_manager import store_api
from autostop_manager.storage import StoreState
from autostop_manager.store_api import StoreApiClient
from autostop_manager.store_integration import StoreIntegration


DOMAIN = "store_batch"
BEFORE = "2026-10-16T10:00:00+00:00"
AFTER = "2026-10-16T10:01:00+00:00"


@pytest.fixture(autouse=True)
def isolate(monkeypatch, tmp_path):
    def deny(*_args, **_kwargs):
        raise AssertionError("identity proof attempted real network")

    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket.socket, "connect_ex", deny)
    monkeypatch.setattr(socket, "create_connection", deny)
    monkeypatch.setattr(socket, "getaddrinfo", deny)
    monkeypatch.setattr(urllib.request, "urlopen", deny)
    monkeypatch.setattr(store_api, "urlopen", deny)
    monkeypatch.setenv("AUTOSTOP_MANAGER_ENV_FILE", os.devnull)
    monkeypatch.setenv("AUTOSTOP_MANAGER_DB", str(tmp_path / "manager.sqlite3"))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))


def envelope(item=None):
    return {
        "ok": True,
        "format": "store_agent_v1",
        "status": "completed",
        "summary": {},
        "items": [] if item is None else [item],
        "changes": [],
        "page": {},
        "warnings": [],
        "meta": {},
    }


class Wire:
    def __init__(self, monkeypatch, tmp_path, target="batch-24", *, uncertain=False):
        self.target = target
        self.uncertain = uncertain
        self.arguments = {
            "domain": DOMAIN,
            "action": "set_batch_storage_location",
            "target_id": target,
            "planned_changes": {"storage_location": "B-2"},
            "owner_intent": "Synthetic owner instruction for the exact test batch",
            "expected_updated_at": BEFORE,
            "idempotency_key": "synthetic-identity-24",
            "correlation_id": "contract:synthetic-identity-24",
            "mode": "apply",
        }
        self.before = {"entity": DOMAIN, "id": target, "updated_at": BEFORE, "storage_location": "A-1"}
        self.after = {"entity": DOMAIN, "id": target, "updated_at": AFTER, "storage_location": "B-2"}
        self.calls = []
        self.responses = []
        self.client = StoreApiClient(
            api_url="https://store.synthetic.example/internal/agent/v1",
            read_token="synthetic-read-token",
            manage_token="synthetic-manage-token",
            max_read_attempts=1,
        )
        self.adapter = StoreIntegration(client=self.client, store=StoreState(tmp_path / "state.sqlite3"))
        monkeypatch.setattr(store_api, "urlopen", self.urlopen)

    def urlopen(self, request, timeout):
        parsed = urlsplit(request.full_url)
        assert parsed.scheme == "https" and parsed.netloc == "store.synthetic.example"
        assert timeout > 0
        method = request.get_method()
        body = json.loads(request.data) if request.data is not None else None
        self.calls.append((method, body))
        index = len(self.calls)
        if index in (1, 3):
            assert method == "GET"
            assert parsed.path == f"/internal/agent/v1/entities/{DOMAIN}/{quote(self.target, safe='')}"
            assert parse_qs(parsed.query) == {"detail": ["summary"]}
            assert request.get_header("Authorization") == "Bearer synthetic-read-token"
            payload = envelope(self.before if index == 1 else self.after)
        else:
            assert index == 2, "the adapter must not repeat POST after an unknown outcome"
            assert method == "POST"
            assert parsed.path == "/internal/agent/v1/actions/set_batch_storage_location"
            assert not parsed.query
            assert request.get_header("Authorization") == "Bearer synthetic-manage-token"
            assert body["target_id"] == self.target
            for name in ("idempotency_key", "correlation_id", "expected_updated_at", "planned_changes"):
                assert body[name] == self.arguments[name]
            if self.uncertain:
                raise TimeoutError("synthetic response lost after dispatch")
            payload = envelope()
        response = BytesIO(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        self.responses.append(response)
        return response

    def call(self):
        original = deepcopy((self.arguments, self.before, self.after))
        result = self.adapter.management_action(**self.arguments)
        assert (self.arguments, self.before, self.after) == original
        assert all(response.closed for response in self.responses)
        return result

    def assert_calls(self, *, preflight_only=False):
        assert [method for method, _ in self.calls] == (["GET"] if preflight_only else ["GET", "POST", "GET"])


def assert_rejected(wire, stage):
    result = wire.call()
    wire.assert_calls(preflight_only=stage == "pre")
    assert result["ok"] is False
    if stage == "pre":
        assert result["status"] == "blocked"
        assert result["summary"]["error_code"] == "store_preflight_target_mismatch"
        return
    assert result["status"] == "compensating"
    assert result["summary"]["write_applied_unverified"] is True
    assert result["meta"]["readback_verified"] is False
    assert result["meta"]["pre_state_read"] is True
    assert result["meta"]["post_state_read"] is True
    if stage == "uncertain":
        assert result["summary"]["error_code"] == "store_apply_outcome_uncertain"
        assert result["meta"]["outcome_uncertain"] is True
        assert result["summary"]["readback_target_matches"] is False
        assert result["summary"]["failed_fields"] == ["storage_location"]
    else:
        assert result["summary"]["error_code"] == "store_apply_readback_target_mismatch"


def assert_verified(wire):
    result = wire.call()
    wire.assert_calls()
    assert result["ok"] is True and result["status"] == "completed"
    assert result["meta"]["readback_verified"] is True
    assert result["meta"]["pre_state_read"] is True
    assert result["meta"]["post_state_read"] is True


@pytest.mark.parametrize("stage", ["pre", "post", "uncertain"])
def test_numeric_wire_id_cannot_establish_exact_string_identity(monkeypatch, tmp_path, stage):
    wire = Wire(monkeypatch, tmp_path, "123", uncertain=stage == "uncertain")
    (wire.before if stage == "pre" else wire.after)["id"] = 123
    assert_rejected(wire, stage)


@pytest.mark.parametrize(
    ("observed", "target"),
    [
        pytest.param(True, "True", id="boolean"),
        pytest.param(1.5, "1.5", id="float"),
        pytest.param([1, "x"], "[1, 'x']", id="array"),
        pytest.param({"slot": 1}, "{'slot': 1}", id="object"),
    ],
)
def test_other_wire_types_cannot_verify_string_identity(monkeypatch, tmp_path, observed, target):
    wire = Wire(monkeypatch, tmp_path, target)
    wire.after["id"] = observed
    assert_rejected(wire, "post")


@pytest.mark.parametrize("stage", ["pre", "post", "uncertain"])
def test_conflicting_nonempty_id_aliases_do_not_establish_one_target(monkeypatch, tmp_path, stage):
    wire = Wire(monkeypatch, tmp_path, uncertain=stage == "uncertain")
    (wire.before if stage == "pre" else wire.after)["entity_id"] = "different-batch"
    assert_rejected(wire, stage)


@pytest.mark.parametrize("target", ["123", "True", "1.5", "[1, 'x']", "{'slot': 1}", "Партия/А 24"])
def test_opaque_string_identity_remains_supported(monkeypatch, tmp_path, target):
    assert_verified(Wire(monkeypatch, tmp_path, target))


@pytest.mark.parametrize("style", ["alternative-only", "both-consistent"])
def test_supported_alias_spellings_remain_valid(monkeypatch, tmp_path, style):
    wire = Wire(monkeypatch, tmp_path)
    for item in (wire.before, wire.after):
        item.update(entity_type=DOMAIN, entity_id=wire.target)
        if style == "alternative-only":
            del item["entity"]
            del item["id"]
    assert_verified(wire)


@pytest.mark.parametrize("empty", [None, ""], ids=["null", "empty-string"])
@pytest.mark.parametrize("primary,alternative", [("id", "entity_id"), ("entity", "entity_type")])
def test_null_or_empty_primary_alias_retains_string_fallback(monkeypatch, tmp_path, primary, alternative, empty):
    wire = Wire(monkeypatch, tmp_path)
    for item in (wire.before, wire.after):
        item[alternative] = item[primary]
        item[primary] = empty
    assert_verified(wire)


def test_matching_unknown_outcome_still_requires_compensation_without_second_post(monkeypatch, tmp_path):
    wire = Wire(monkeypatch, tmp_path, uncertain=True)
    result = wire.call()
    wire.assert_calls()
    assert result["ok"] is False and result["status"] == "compensating"
    assert result["summary"]["error_code"] == "store_apply_outcome_uncertain"
    assert result["summary"]["write_applied_unverified"] is True
    assert result["summary"]["readback_target_matches"] is True
    assert result["summary"]["failed_fields"] == []
    assert result["meta"]["outcome_uncertain"] is True
    assert result["meta"]["readback_verified"] is False
