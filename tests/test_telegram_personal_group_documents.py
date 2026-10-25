"""Opt-in PDF replies use synthetic Telegram peers and private temporary files."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import replace
from types import SimpleNamespace

import pytest
from test_telegram_bridge import _minimal_pdf

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
    outbox = config.socket_path.parent / "outbox"
    outbox.mkdir(parents=True, mode=0o700)
    document = outbox / "synthetic-diagram.pdf"
    content = _minimal_pdf(note=b"synthetic-diagram")
    document.write_bytes(content)
    document.chmod(0o600)
    state = SimpleNamespace(
        config=config,
        target={"id": GROUP_ID, "kind": "supergroup"},
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
        document=document,
        content=content,
        uploaded=b"",
        last_id=20,
        expected_reply=5,
        readback={},
        missing_readback=False,
        downloaded=None,
        downloads=0,
        sends=[],
        reads=[],
        uncertain=False,
        swap_file=False,
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
            if ids in {5, 6}:
                return state.source
            assert ids == 30
            if state.missing_readback:
                return None
            return SimpleNamespace(
                **(
                    {
                        "id": 30,
                        "chat_id": GROUP_ID,
                        "is_private": False,
                        "is_group": True,
                        "out": True,
                        "message": "diagram",
                        "reply_to_msg_id": 5,
                        "action": None,
                        "media": SimpleNamespace(),
                        "document": SimpleNamespace(id=123, attributes=[]),
                        "file": SimpleNamespace(name=document.name, mime_type="application/pdf", size=len(content)),
                    }
                    | state.readback
                )
            )

        async def send_file(self, actual_entity, upload, **kwargs):
            assert actual_entity is entity
            assert kwargs == {
                "caption": "diagram",
                "force_document": True,
                "mime_type": "application/pdf",
                "parse_mode": None,
                **({"reply_to": state.expected_reply} if state.expected_reply else {}),
            }
            if state.swap_file:
                document.write_bytes(_minimal_pdf(note=b"changed-during-upload"))
            state.uploaded = upload.read()
            state.sends.append((upload.name, state.uploaded))
            state.last_id = 30
            if state.uncertain:
                raise TimeoutError("synthetic unknown document outcome")
            return SimpleNamespace(id=30)

        async def download_media(self, message, *, file):
            assert message.id == 30 and file is bytes
            state.downloads += 1
            return state.uploaded if state.downloaded is None else state.downloaded

    monkeypatch.setattr(bridge, "_resolve_peer", resolve)
    state.client = Client()
    state.request = {
        "operation": "send_document",
        "peer": str(GROUP_ID),
        "document": str(document),
        "caption": "diagram",
        "reply_to_message_id": 5,
        "mode": "dry_run",
    }

    def invoke(request=None, **kwargs):
        return asyncio.run(bridge._handle_operation(state.client, state.config, request or state.request, **kwargs))

    state.invoke = invoke
    return state


def _apply_request(scenario):
    dry = scenario.invoke()
    assert dry["reply_source"]["message_id"] == 5
    return scenario.request | {
        "mode": "apply",
        "contract_token": dry["contract_token"],
        "idempotency_key": "synthetic-group-pdf",
    }


def test_pdf_reply_and_replay_verify_bytes_without_resending(scenario):
    request = _apply_request(scenario)
    result = scenario.invoke(request)
    scenario.last_id = 31
    replay = scenario.invoke(request)
    assert result["verified"] and result["reply_verified"]
    assert result["reply_to_message_id"] == 5
    assert result["document_sha256"] == hashlib.sha256(scenario.content).hexdigest()
    assert result["message_id"] == replay["message_id"] == 30
    assert replay["replayed"] and not result["replayed"]
    assert scenario.sends == [(scenario.document.name, scenario.content)]
    assert scenario.downloads == 2
    assert scenario.reads.count((30, None)) == 2


@pytest.mark.parametrize("account", [None, "work"])
def test_pdf_group_requires_explicit_personal_account(scenario, account):
    scenario.config = replace(scenario.config, account=account)
    with pytest.raises(bridge.BridgeError, match="exact_private_peer_required"):
        scenario.invoke()
    assert not scenario.reads and not scenario.sends


def test_pdf_group_permission_cannot_come_from_rpc_or_client_environment(scenario, monkeypatch):
    scenario.config = replace(scenario.config, personal_group_reply_peer_ids=frozenset())
    monkeypatch.setenv(bridge.PERSONAL_GROUP_REPLY_ENVIRONMENT, str(GROUP_ID))
    with pytest.raises(bridge.BridgeError, match="exact_private_peer_required"):
        scenario.invoke(scenario.request | {"personal_group_reply_peer_ids": [GROUP_ID]})
    assert not scenario.reads and not scenario.sends
    with pytest.raises(bridge.BridgeError, match="exact_private_peer_required"):
        bridge.validate_document_request(scenario.request)


@pytest.mark.parametrize("kind", ["private", "channel", "group", "bot", "unknown"])
def test_pdf_negative_peer_cannot_fall_through_to_another_entity_kind(scenario, kind):
    scenario.target["kind"] = kind
    with pytest.raises(bridge.BridgeError, match="private_peer_required"):
        scenario.invoke()
    assert not scenario.reads and not scenario.sends


@pytest.mark.parametrize("peer", ["@synthetic", "Synthetic group", f" {GROUP_ID}", f"{GROUP_ID} ", str(-GROUP_ID)])
def test_pdf_group_requires_canonical_numeric_request_peer(scenario, peer):
    with pytest.raises(bridge.BridgeError, match=r"exact_(private|supergroup)_peer_required"):
        scenario.invoke(scenario.request | {"peer": peer})
    assert not scenario.reads and not scenario.sends


def test_pdf_resolved_group_must_equal_the_requested_allowed_peer(scenario):
    scenario.config = replace(scenario.config, personal_group_reply_peer_ids=frozenset({GROUP_ID, OTHER_GROUP_ID}))
    scenario.target["id"] = OTHER_GROUP_ID
    with pytest.raises(bridge.BridgeError, match="exact_supergroup_peer_required"):
        scenario.invoke()
    assert not scenario.reads and not scenario.sends


@pytest.mark.parametrize("reply", [0, -1, True, False, 5.0, "5", None])
def test_pdf_group_requires_positive_integer_reply(scenario, reply):
    with pytest.raises(bridge.BridgeError, match=r"group_reply_required|reply_to_message_id_invalid"):
        scenario.invoke(scenario.request | {"reply_to_message_id": reply})
    assert not scenario.reads and not scenario.sends


@pytest.mark.parametrize(
    "field", ["job_id", "lease", "revision", "kind", "effect_slot", "owner_notification", "account"]
)
def test_pdf_group_rejects_job_owner_and_rpc_account_routes(scenario, field):
    with pytest.raises(bridge.BridgeError, match="private_peer_required"):
        scenario.invoke(scenario.request | {field: "synthetic"})
    assert not scenario.reads and not scenario.sends


def test_pdf_group_rejects_inbound_monitor_and_work_owner_configuration(scenario, tmp_path):
    with pytest.raises(bridge.BridgeError, match="private_peer_required"):
        scenario.invoke(inbound_monitor=bridge.InboundMonitor())
    scenario.config = replace(scenario.config, owner_config_path=tmp_path / "owner.json")
    with pytest.raises(bridge.BridgeError, match="private_peer_required"):
        scenario.invoke()
    assert not scenario.reads and not scenario.sends


@pytest.mark.parametrize(
    "change",
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
def test_pdf_reply_source_must_be_an_ordinary_message_in_the_exact_group(scenario, change):
    scenario.source = None if change is None else SimpleNamespace(**(vars(scenario.source) | change))
    with pytest.raises(bridge.BridgeError, match="reply_source_not_found"):
        scenario.invoke()
    assert not scenario.sends
    assert not (scenario.config.state_dir / "idempotency.json").exists()


@pytest.mark.parametrize(
    ("change", "error"),
    [
        ("latest", "conversation_changed_since_dry_run"),
        ("text", "contract_reply_source_changed"),
        ("media", "contract_reply_source_changed"),
        ("caption", "contract_text_changed"),
        ("file", "contract_document_changed"),
        ("reply", "contract_reply_target_changed"),
    ],
)
def test_pdf_apply_requires_the_original_file_caption_reply_and_fresh_source(scenario, change, error):
    request = _apply_request(scenario)
    if change == "latest":
        scenario.last_id += 1
    elif change == "text":
        scenario.source.message = "edited question"
    elif change == "media":
        scenario.source.media = object()
    elif change == "caption":
        request["caption"] = "edited caption"
    elif change == "file":
        scenario.document.write_bytes(_minimal_pdf(note=b"edited-diagram"))
    else:
        scenario.source.id = request["reply_to_message_id"] = 6
    with pytest.raises(bridge.BridgeError, match=error):
        scenario.invoke(request)
    assert not scenario.sends
    assert not (scenario.config.state_dir / "idempotency.json").exists()


@pytest.mark.parametrize("stage", ["apply", "replay"])
def test_pdf_permission_is_rechecked_before_apply_and_replay(scenario, stage):
    request = _apply_request(scenario)
    if stage == "replay":
        scenario.invoke(request)
    scenario.config = replace(scenario.config, personal_group_reply_peer_ids=frozenset())
    with pytest.raises(bridge.BridgeError, match="exact_private_peer_required"):
        scenario.invoke(request)
    assert len(scenario.sends) == (1 if stage == "replay" else 0)


@pytest.mark.parametrize(
    "change",
    [
        None,
        {"id": 31},
        {"id": "30"},
        {"chat_id": OTHER_GROUP_ID},
        {"chat_id": str(GROUP_ID)},
        {"is_private": True},
        {"is_group": False},
        {"out": False},
        {"message": "changed caption"},
        {"reply_to_msg_id": 6},
        {"action": object()},
        {"media": None},
        {"document": None},
        {"photo": object()},
        {"file": SimpleNamespace(name="different.pdf", mime_type="application/pdf", size=1)},
    ],
)
def test_pdf_readback_must_match_envelope_reply_caption_and_media(scenario, change):
    request = _apply_request(scenario)
    if change is None:
        scenario.missing_readback = True
    else:
        scenario.readback = change
    for _ in range(2):
        with pytest.raises(bridge.BridgeError, match="send_document_readback_failed"):
            scenario.invoke(request)
    assert len(scenario.sends) == 1
    scenario.readback = {}
    scenario.missing_readback = False
    assert scenario.invoke(request)["replayed"]
    assert len(scenario.sends) == 1


def test_pdf_same_length_download_substitution_fails_sha_check_and_never_resends(scenario):
    request = _apply_request(scenario)
    scenario.downloaded = scenario.content[:-1] + b"?"
    assert len(scenario.downloaded) == len(scenario.content)
    for _ in range(2):
        with pytest.raises(bridge.BridgeError, match="send_document_readback_failed"):
            scenario.invoke(request)
    assert len(scenario.sends) == 1 and scenario.downloads == 2
    entry = bridge._load_idempotency(scenario.config.state_dir / "idempotency.json")[request["idempotency_key"]]
    assert entry["message_id"] == 30 and not entry["readback_verified"]
    scenario.downloaded = None
    assert scenario.invoke(request)["verified"]
    assert len(scenario.sends) == 1


def test_pdf_unknown_send_outcome_blocks_even_fresh_contract_retry_with_the_same_key(scenario):
    request = _apply_request(scenario)
    scenario.uncertain = True
    with pytest.raises(TimeoutError):
        scenario.invoke(request)
    fresh = _apply_request(scenario)
    with pytest.raises(bridge.BridgeError, match="send_document_outcome_uncertain"):
        scenario.invoke(fresh)
    assert len(scenario.sends) == 1


def test_pdf_upload_uses_validated_snapshot_even_if_the_local_file_changes_during_send(scenario):
    request = _apply_request(scenario)
    scenario.swap_file = True
    assert scenario.invoke(request)["verified"]
    assert scenario.uploaded == scenario.content
    assert scenario.document.read_bytes() != scenario.content


@pytest.mark.parametrize(
    ("change", "error"),
    [
        ("permissions", "document_permissions_invalid"),
        ("symlink", "document_unavailable"),
        ("non_pdf", "document_invalid"),
    ],
)
def test_group_pdf_keeps_existing_private_file_safeguards(scenario, change, error):
    if change == "permissions":
        scenario.document.chmod(0o644)
    elif change == "symlink":
        target = scenario.document.with_name("other.pdf")
        scenario.document.rename(target)
        scenario.document.symlink_to(target.name)
    else:
        scenario.document.write_bytes(b"not a PDF")
    with pytest.raises(bridge.BridgeError, match=error):
        scenario.invoke()
    assert not scenario.reads and not scenario.sends


def test_private_document_path_and_standalone_reply_behavior_are_unchanged(scenario):
    scenario.target = {"id": 42, "kind": "private"}
    scenario.request["peer"] = "42"
    scenario.expected_reply = 0
    scenario.readback = {"chat_id": 42, "is_private": True, "is_group": False, "reply_to_msg_id": 0}
    dry = scenario.invoke()
    assert "reply_source" not in dry and not any(ids == 5 for ids, _limit in scenario.reads)
    result = scenario.invoke(
        scenario.request
        | {
            "mode": "apply",
            "contract_token": dry["contract_token"],
            "idempotency_key": "synthetic-private-pdf",
        }
    )
    assert result["verified"] and len(scenario.sends) == 1


def test_personal_document_cli_preserves_standalone_group_reply(monkeypatch, tmp_path, capsys):
    requests = []
    monkeypatch.setattr(bridge, "send_local_request", lambda _socket, request: requests.append(request) or {"ok": True})
    assert (
        bridge.main(
            [
                "--account",
                "personal",
                "send-document",
                f"--peer={GROUP_ID}",
                "--file",
                str(tmp_path / "synthetic.pdf"),
                "--caption",
                "diagram",
                "--reply-to-message-id",
                "5",
            ]
        )
        == 0
    )
    assert requests == [
        {
            "operation": "send_document",
            "peer": str(GROUP_ID),
            "document": str(tmp_path / "synthetic.pdf"),
            "caption": "diagram",
            "mode": "dry_run",
            "contract_token": "",
            "idempotency_key": "",
            "reply_to_message_id": 5,
        }
    ]
    assert '"ok": true' in capsys.readouterr().out
