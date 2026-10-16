"""Typed Store revision evidence with legacy opaque-string compatibility.

Twenty distinct scenarios, twenty pytest runs, and no native-interface repeats.
Real action-contract, StoreApiClient JSON parsing/projection and integration flow
use synthetic urllib responses. No address is contacted and no business action
or Manager database write occurs. JSON revision types remain distinct from
literal opaque strings; changing opaque strings makes no token-ordering claim.
"""

from __future__ import annotations

from copy import deepcopy
from io import BytesIO
import json
from urllib.parse import parse_qs, urlsplit

import pytest

from autostop_manager import store_api
from autostop_manager.storage import StoreState
from autostop_manager.store_api import StoreApiClient
from autostop_manager.store_integration import StoreIntegration


BEFORE = "2026-10-16T10:00:00+00:00"
MALFORMED = "2026-02-30T10:01:00+00:00"


def _envelope(item=None, *, replay=False):
    return {
        "ok": True,
        "format": "store_agent_v1",
        "status": "completed",
        "summary": {},
        "items": [] if item is None else [item],
        "changes": [],
        "page": {},
        "warnings": [],
        "meta": {"idempotency_replay": True} if replay else {},
    }


def _run(
    monkeypatch,
    tmp_path,
    *,
    before=BEFORE,
    after,
    replay=False,
    uncertain=False,
    request_expected=None,
    expected_methods=("GET", "POST", "GET"),
):
    target = "synthetic-batch-32"
    entity = "store_batch"
    operation = "set_batch_storage_location"
    before_item = {"entity": entity, "id": target, "updated_at": before, "storage_location": "Z-0"}
    after_item = {**before_item, "updated_at": after, "storage_location": "A-1"}
    arguments = {
        "domain": entity,
        "action": operation,
        "target_id": target,
        "planned_changes": {"storage_location": "A-1"},
        "owner_intent": "Synthetic exact batch storage-location instruction",
        "expected_updated_at": before if request_expected is None else request_expected,
        "idempotency_key": "synthetic-version-32",
        "correlation_id": "contract:synthetic-version-32",
        "mode": "apply",
    }
    snapshot = deepcopy((arguments, before_item, after_item))
    calls = []
    response_streams = []

    def fake_urlopen(request, timeout):
        parsed = urlsplit(request.full_url)
        assert parsed.scheme == "https" and parsed.netloc == "store.synthetic.example"
        assert timeout > 0
        method = request.get_method()
        body = json.loads(request.data) if request.data is not None else None
        calls.append((method, parsed.path, body))
        sequence = len(calls)
        if sequence in (1, 3):
            assert method == "GET"
            assert parsed.path == f"/internal/agent/v1/entities/{entity}/{target}"
            assert parse_qs(parsed.query) == {"detail": ["summary"]}
            assert request.get_header("Authorization") == "Bearer synthetic-read-token"
            payload = _envelope(before_item if sequence == 1 else after_item)
        else:
            assert sequence == 2, "unexpected retry or second POST"
            assert method == "POST" and parsed.path == f"/internal/agent/v1/actions/{operation}"
            assert not parsed.query
            assert request.get_header("Authorization") == "Bearer synthetic-manage-token"
            if uncertain:
                raise TimeoutError("synthetic response loss after dispatch")
            payload = _envelope(replay=replay)
        stream = BytesIO(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        response_streams.append(stream)
        return stream

    monkeypatch.setattr(store_api, "urlopen", fake_urlopen)
    state_path = tmp_path / "unused-manager-state.sqlite3"
    client = StoreApiClient(
        api_url="https://store.synthetic.example/internal/agent/v1",
        read_token="synthetic-read-token",
        manage_token="synthetic-manage-token",
        max_read_attempts=1,
    )
    result = StoreIntegration(client=client, store=StoreState(state_path)).management_action(**arguments)
    assert (arguments, before_item, after_item) == snapshot
    assert all(stream.closed for stream in response_streams)
    assert not state_path.exists()
    posts = [body for method, _, body in calls if method == "POST"]
    assert len(posts) <= 1
    if posts:
        sent = posts[0]
        for name in (
            "target_id",
            "expected_updated_at",
            "owner_intent",
            "idempotency_key",
            "correlation_id",
            "mode",
            "planned_changes",
        ):
            assert sent[name] == arguments[name]
    # The transport also supports a forbidden baseline POST; this assertion
    # detects dispatch itself instead of making the transport fixture fail.
    assert [method for method, _, _ in calls] == list(expected_methods)
    return result


@pytest.mark.parametrize(
    "before,after,replay,verified",
    [
        pytest.param(BEFORE, MALFORMED, False, False, id="invalid-calendar-date"),
        pytest.param(BEFORE, True, False, False, id="json-boolean"),
        pytest.param(BEFORE, 123, False, False, id="json-number"),
        pytest.param(BEFORE, {"revision": 2}, False, False, id="json-object"),
        pytest.param(BEFORE, "2026-10-16T10:00:00.000001Z", False, True, id="one-microsecond-later"),
        pytest.param(BEFORE, "2026-10-16T17:00:00+07:00", False, False, id="same-instant-other-offset"),
        pytest.param(BEFORE, "2026-10-16T16:59:59+07:00", False, False, id="older-instant-later-wall-clock"),
        pytest.param(BEFORE, "2026-10-16T10:01:00", False, False, id="mixed-naive-aware"),
        pytest.param("version-1", "version-2", False, True, id="legacy-opaque-changed"),
        pytest.param("version-1", "version-1", False, False, id="legacy-opaque-unchanged"),
        pytest.param(BEFORE, BEFORE, True, True, id="exact-idempotency-replay"),
        pytest.param("version-1", True, False, False, id="opaque-before-json-boolean"),
        pytest.param(BEFORE, {"revision": 2}, True, False, id="replay-json-object"),
        pytest.param("version-1", "True", False, True, id="opaque-string-boolean-looking"),
        pytest.param("version-1", "123", False, True, id="opaque-string-number-looking"),
        pytest.param("version-1", "{'revision': 2}", False, True, id="opaque-string-object-looking"),
        pytest.param("2", "1", False, True, id="opaque-string-change-is-not-numeric-order"),
        pytest.param(BEFORE, MALFORMED, True, True, id="replay-does-not-add-timestamp-format-contract"),
    ],
)
def test_public_readback_version_evidence(monkeypatch, tmp_path, before, after, replay, verified):
    result = _run(monkeypatch, tmp_path, before=before, after=after, replay=replay)
    assert result["meta"]["pre_state_read"] is True
    assert result["meta"]["post_state_read"] is True
    assert result["meta"]["pre_read_revision_mismatch"] is False
    if verified:
        assert result["ok"] is True
        assert result["meta"]["readback_verified"] is True
        if replay:
            assert result["meta"]["idempotency_replay"] is True
    else:
        assert result["ok"] is False
        assert result["status"] == "compensating"
        assert result["summary"]["error_code"] == "store_apply_readback_version_not_advanced"
        assert result["summary"]["write_applied_unverified"] is True
        assert result["meta"]["readback_verified"] is False
        assert result["meta"]["idempotency_replay"] is replay


def test_uncertain_post_stays_unverified_after_matching_fields_and_malformed_version(monkeypatch, tmp_path):
    result = _run(monkeypatch, tmp_path, after=MALFORMED, uncertain=True)
    assert result["ok"] is False
    assert result["status"] == "compensating"
    assert result["summary"]["error_code"] == "store_apply_outcome_uncertain"
    assert result["summary"]["write_applied_unverified"] is True
    assert result["summary"]["readback_target_matches"] is True
    assert result["summary"]["failed_fields"] == []
    assert result["meta"]["request_dispatched"] is True
    assert result["meta"]["outcome_uncertain"] is True
    assert result["meta"]["post_state_read"] is True
    assert result["meta"]["readback_verified"] is False


def test_non_string_preread_is_blocked_before_post_even_when_request_string_matches(monkeypatch, tmp_path):
    result = _run(
        monkeypatch,
        tmp_path,
        before=True,
        after="version-2",
        request_expected="True",
        expected_methods=("GET",),
    )
    assert result["ok"] is False
    assert result["status"] == "blocked"
    assert result["summary"]["error_code"] == "store_preflight_version_missing"
