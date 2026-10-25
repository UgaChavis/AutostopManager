"""Exact opt-in group text replies; all Telegram operations are synthetic."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from autostop_manager import telegram_bridge as bridge


GROUP_ID = -1001112223334
OTHER_GROUP_ID = -1005556667778


@pytest.fixture
def scenario(tmp_path, monkeypatch):
    config = bridge.TelegramConfig(
        123456,
        "0" * 32,
        session_path=tmp_path / "state" / "account",
        state_dir=tmp_path / "state",
        socket_path=tmp_path / "run" / "bridge.sock",
        account="personal",
        personal_group_reply_peer_ids=frozenset({GROUP_ID}),
    )
    state = SimpleNamespace(
        config=config,
        target={"id": GROUP_ID, "kind": "supergroup"},
        last_id=20,
        source=SimpleNamespace(
            id=5,
            chat_id=GROUP_ID,
            is_private=False,
            is_group=True,
            message="question",
            media=None,
            action=None,
            out=False,
            date=None,
        ),
        readback={},
        sends=[],
        uncertain=False,
        reads=[],
    )
    entity = object()

    async def resolve(_client, _peer):
        return entity, state.target

    class Client:
        async def get_messages(self, actual_entity, *, ids=None, limit=None):
            assert actual_entity is entity
            state.reads.append((ids, limit))
            if limit is not None:
                assert limit == 1
                return [SimpleNamespace(id=state.last_id)]
            if ids == 5:
                return state.source
            assert ids == 30
            return SimpleNamespace(
                **(
                    {
                        "id": 30,
                        "chat_id": GROUP_ID,
                        "is_private": False,
                        "is_group": True,
                        "out": True,
                        "message": "answer",
                        "reply_to_msg_id": 5,
                        "media": None,
                        "action": None,
                    }
                    | state.readback
                )
            )

        async def send_message(self, actual_entity, text, **kwargs):
            assert actual_entity is entity
            assert kwargs == {"parse_mode": None, "reply_to": 5, "link_preview": False}
            state.sends.append(text)
            if state.uncertain:
                raise TimeoutError("synthetic unknown send outcome")
            return SimpleNamespace(id=30)

    monkeypatch.setattr(bridge, "_resolve_peer", resolve)
    state.client = Client()
    state.request = {
        "operation": "send",
        "peer": str(GROUP_ID),
        "text": "answer",
        "reply_to_message_id": 5,
        "mode": "dry_run",
    }

    def invoke(request=None, **kwargs):
        return asyncio.run(bridge._handle_operation(state.client, state.config, request or state.request, **kwargs))

    state.invoke = invoke
    return state


def _apply_request(scenario):
    dry = scenario.invoke()
    return scenario.request | {
        "mode": "apply",
        "contract_token": dry["contract_token"],
        "idempotency_key": "synthetic-group-reply",
    }


@pytest.mark.parametrize("account", [None, "work"])
def test_group_permission_requires_explicit_personal_account(scenario, account):
    scenario.config = replace(scenario.config, account=account)
    with pytest.raises(bridge.BridgeError, match="private_peer_required"):
        scenario.invoke()
    assert scenario.reads == []
    assert scenario.sends == []


def test_default_deny_cannot_be_overridden_by_rpc_or_client_environment(scenario, monkeypatch):
    scenario.config = replace(scenario.config, personal_group_reply_peer_ids=frozenset())
    monkeypatch.setenv(bridge.PERSONAL_GROUP_REPLY_ENVIRONMENT, str(GROUP_ID))
    request = scenario.request | {"account": "personal", "personal_group_reply_peer_ids": [GROUP_ID]}
    with pytest.raises(bridge.BridgeError, match="private_peer_required"):
        scenario.invoke(request)
    assert scenario.reads == []
    assert scenario.sends == []


@pytest.mark.parametrize("kind", ["channel", "group", "bot", "unknown"])
def test_allowlisted_id_does_not_authorize_other_entity_kinds(scenario, kind):
    scenario.target["kind"] = kind
    with pytest.raises(bridge.BridgeError, match="private_peer_required"):
        scenario.invoke()
    assert scenario.sends == []


def test_other_supergroup_is_denied(scenario):
    scenario.target["id"] = OTHER_GROUP_ID
    with pytest.raises(bridge.BridgeError, match="private_peer_required"):
        scenario.invoke(scenario.request | {"peer": str(OTHER_GROUP_ID)})
    assert scenario.sends == []


@pytest.mark.parametrize("peer", ["@synthetic_group", "Group title", str(GROUP_ID)[1:], f" {GROUP_ID}"])
def test_group_requires_exact_canonical_numeric_request_peer(scenario, peer):
    with pytest.raises(bridge.BridgeError, match="exact_supergroup_peer_required"):
        scenario.invoke(scenario.request | {"peer": peer})
    assert scenario.reads == []


def test_group_requires_reply_and_disallows_work_monitor(scenario):
    with pytest.raises(bridge.BridgeError, match="group_reply_required"):
        scenario.invoke(scenario.request | {"reply_to_message_id": 0})
    with pytest.raises(bridge.BridgeError, match="private_peer_required"):
        scenario.invoke(inbound_monitor=bridge.InboundMonitor())
    assert scenario.reads == []
    assert scenario.sends == []


@pytest.mark.parametrize(
    "override",
    [
        {"bound_effect": {"effect_key": "synthetic-effect"}},
        {"owner_notification": True},
        {"contract_target_role": "owner"},
        {"idempotency_operation": "send_owner_notification"},
    ],
)
def test_group_permission_does_not_open_internal_work_or_owner_routes(scenario, override):
    with pytest.raises(bridge.BridgeError, match="private_peer_required"):
        asyncio.run(
            bridge._handle_send_text_to_entity(
                scenario.client,
                scenario.config,
                scenario.request,
                entity=object(),
                target=scenario.target,
                text="answer",
                mode="dry_run",
                idempotency_key="",
                reply_to_message_id=5,
                **override,
            )
        )
    assert scenario.reads == []


@pytest.mark.parametrize(
    "source_override",
    [
        None,
        {"id": 6},
        {"id": "5"},
        {"chat_id": OTHER_GROUP_ID},
        {"chat_id": str(GROUP_ID)},
        {"is_private": True},
        {"is_group": False},
        {"action": object()},
        {"message": None},
    ],
)
def test_group_reply_source_must_exist_and_belong_to_the_exact_group(scenario, source_override):
    if source_override is None:
        scenario.source = None
    else:
        scenario.source = SimpleNamespace(**(vars(scenario.source) | source_override))
    with pytest.raises(bridge.BridgeError, match="reply_source_not_found"):
        scenario.invoke()
    assert scenario.sends == []
    assert not (scenario.config.state_dir / "idempotency.json").exists()


def test_group_text_reply_applies_and_replay_verifies_original_message_without_resending(scenario):
    request = _apply_request(scenario)
    result = scenario.invoke(request)
    scenario.last_id = 31  # New chat activity must not turn a receipt check into a new send.
    replay = scenario.invoke(request)
    assert result["verified"] is True
    assert result["replayed"] is False
    assert result["message_id"] == replay["message_id"] == 30
    assert result["target"]["id"] == GROUP_ID
    assert replay["replayed"] is True
    assert replay["reply_verified"] is True
    assert scenario.sends == ["answer"]
    assert scenario.reads.count((30, None)) == 2


def test_group_apply_and_replay_recheck_current_configuration(scenario):
    request = _apply_request(scenario)
    scenario.config = replace(scenario.config, personal_group_reply_peer_ids=frozenset())
    with pytest.raises(bridge.BridgeError, match="private_peer_required"):
        scenario.invoke(request)
    assert scenario.sends == []
    scenario.config = replace(scenario.config, personal_group_reply_peer_ids=frozenset({GROUP_ID}))
    scenario.invoke(request)
    scenario.config = replace(scenario.config, account="work")
    with pytest.raises(bridge.BridgeError, match="private_peer_required"):
        scenario.invoke(request)
    assert scenario.sends == ["answer"]


def test_group_apply_rejects_changed_resolved_target(scenario):
    request = _apply_request(scenario)
    scenario.config = replace(scenario.config, personal_group_reply_peer_ids=frozenset({GROUP_ID, OTHER_GROUP_ID}))
    scenario.target = {"id": OTHER_GROUP_ID, "kind": "supergroup"}
    with pytest.raises(bridge.BridgeError, match="exact_supergroup_peer_required"):
        scenario.invoke(request)
    assert scenario.sends == []


@pytest.mark.parametrize("change", ["last_message", "source_text", "source_media"])
def test_group_apply_requires_fresh_conversation_and_reply_source(scenario, change):
    request = _apply_request(scenario)
    if change == "last_message":
        scenario.last_id = 21
        expected = "conversation_changed_since_dry_run"
    else:
        if change == "source_text":
            scenario.source.message = "edited question"
        else:
            scenario.source.media = object()
        expected = "contract_reply_source_changed"
    with pytest.raises(bridge.BridgeError, match=expected):
        scenario.invoke(request)
    assert scenario.sends == []
    assert not (scenario.config.state_dir / "idempotency.json").exists()


@pytest.mark.parametrize(
    "readback_override",
    [
        {"id": 31},
        {"chat_id": OTHER_GROUP_ID},
        {"is_private": True},
        {"is_group": False},
        {"out": False},
        {"message": "other answer"},
        {"reply_to_msg_id": 6},
        {"media": object()},
        {"action": object()},
    ],
)
def test_group_readback_is_exact_and_failed_readback_never_resends(scenario, readback_override):
    request = _apply_request(scenario)
    scenario.readback = readback_override
    for _ in range(2):
        with pytest.raises(bridge.BridgeError, match="send_readback_failed"):
            scenario.invoke(request)
    assert scenario.sends == ["answer"]
    scenario.readback = {}
    assert scenario.invoke(request)["replayed"] is True
    assert scenario.sends == ["answer"]


def test_group_unknown_send_outcome_preserves_intent_and_blocks_retry(scenario):
    request = _apply_request(scenario)
    scenario.uncertain = True
    with pytest.raises(TimeoutError):
        scenario.invoke(request)
    with pytest.raises(bridge.BridgeError, match="send_outcome_uncertain"):
        scenario.invoke(request)
    assert scenario.sends == ["answer"]


def test_group_allowlist_is_loaded_only_in_personal_daemon_config(monkeypatch):
    loaded = []

    def load(_credentials, **kwargs):
        loaded.append(kwargs)
        return kwargs

    monkeypatch.setattr(bridge.TelegramConfig, "load", load)
    monkeypatch.setenv(bridge.PERSONAL_GROUP_REPLY_ENVIRONMENT, f"{GROUP_ID}, {OTHER_GROUP_ID}")
    personal = bridge._config_for_account("personal")
    work = bridge._config_for_account("work")
    assert personal["account"] == "personal"
    assert personal["personal_group_reply_peer_ids"] == frozenset({GROUP_ID, OTHER_GROUP_ID})
    assert work["account"] == "work"
    assert work["personal_group_reply_peer_ids"] == frozenset()
    assert len(loaded) == 2


@pytest.mark.parametrize(
    "raw",
    ["@group", "12", "-123", "-01001112223334", f"{GROUP_ID},", "-9223372036854775809", "1" * 1000],
)
def test_invalid_group_allowlist_fails_closed(monkeypatch, raw):
    monkeypatch.setenv(bridge.PERSONAL_GROUP_REPLY_ENVIRONMENT, raw)
    with pytest.raises(bridge.BridgeError, match="personal_group_reply_config_invalid"):
        bridge._personal_group_reply_peers("personal")
    assert bridge._personal_group_reply_peers("work") == frozenset()


def test_group_allowlist_defaults_empty(monkeypatch):
    monkeypatch.delenv(bridge.PERSONAL_GROUP_REPLY_ENVIRONMENT, raising=False)
    assert bridge._personal_group_reply_peers("personal") == frozenset()
    assert bridge.TelegramConfig(123456, "0" * 32).personal_group_reply_peer_ids == frozenset()


@pytest.mark.parametrize("operation", ["send_photo", "send_voice"])
def test_group_allowlist_does_not_open_photo_or_voice_send_paths(scenario, monkeypatch, operation):
    monkeypatch.setattr(bridge, "_load_validated_photo_file", lambda *_args, **_kwargs: (None, "0" * 64, 8, b"photo"))
    request = {
        "operation": operation,
        "peer": str(GROUP_ID),
        "caption": "caption",
        "mode": "dry_run",
    }
    media_field = {"send_photo": "photo", "send_document": "document", "send_voice": "voice"}[operation]
    request[media_field] = "synthetic-file"
    with pytest.raises(bridge.BridgeError, match=r"private_peer_required|exact_private_peer_required"):
        scenario.invoke(request)
    assert scenario.sends == []
