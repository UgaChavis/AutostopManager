"""Preserve Telegram's double video duration through metadata and effect guards."""

from copy import deepcopy
from datetime import UTC, datetime
import hashlib
import json
import math
import socket
import sqlite3
import subprocess
from types import SimpleNamespace

import pytest
from telethon.extensions import BinaryReader
from telethon.tl.types import (
    Document,
    DocumentAttributeAudio,
    DocumentAttributeFilename,
    DocumentAttributeVideo,
    Message,
    MessageMediaDocument,
    MessageReplyHeader,
    PeerUser,
)

from autostop_manager import telegram_bridge as bridge

SECRET = b"synthetic-video-duration-contract-key"
CONTENT = b"\x00\x00\x00\x18ftypisomsynthetic-video"
CONTENT_SIZE = len(CONTENT)
DATE = datetime(2026, 1, 1, tzinfo=UTC)
TARGET = {"id": 10, "kind": "private", "title": "Synthetic"}


def complete(coroutine):
    """All fake transport calls finish immediately, without sockets/event loops."""
    try:
        coroutine.send(None)
    except StopIteration as result:
        return result.value
    finally:
        coroutine.close()
    pytest.fail("synthetic transport unexpectedly suspended")


def video_message(duration, *, size=CONTENT_SIZE, width=640, height=360):
    # Exercise the installed TL double deserializer, not a hand-written attribute.
    with BinaryReader(bytes(DocumentAttributeVideo(duration=duration, w=width, h=height))) as reader:
        attribute = reader.tgread_object()
    assert type(attribute.duration) is float
    document = Document(
        id=123,
        access_hash=456,
        file_reference=b"synthetic",
        date=DATE,
        mime_type="video/mp4",
        size=size,
        dc_id=2,
        attributes=[attribute, DocumentAttributeFilename("synthetic.mp4")],
    )
    return Message(
        id=48,
        peer_id=PeerUser(10),
        date=DATE,
        message="Synthetic source",
        media=MessageMediaDocument(document=document),
    )


def legacy_fingerprint(duration):
    """The baseline's canonical payload for the fixed source fixture, before migration."""
    payload = {
        "file_name": "synthetic.mp4",
        "media_type": "MessageMediaDocument",
        "mime_type": "video/mp4",
        "size_bytes": len(CONTENT),
        "suffix": ".mp4",
        "duration_seconds": int(duration),
        "voice": False,
        "video": True,
        "width": 640,
        "height": 360,
        "media_id": 123,
        "dc_id": 2,
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


@pytest.fixture(autouse=True)
def no_external_activity(monkeypatch):
    def denied(*_args, **_kwargs):
        raise AssertionError("video duration test attempted external activity")

    for owner, name in (
        (socket.socket, "connect"),
        (socket.socket, "connect_ex"),
        (socket, "create_connection"),
        (socket, "getaddrinfo"),
        (sqlite3, "connect"),
        (subprocess, "Popen"),
    ):
        monkeypatch.setattr(owner, name, denied)
    monkeypatch.setattr(bridge, "_ensure_private_key", lambda _path: SECRET)


@pytest.fixture
def runtime(monkeypatch, tmp_path):
    state = SimpleNamespace(ledger={}, reads=[], downloads=0, saves=0)

    async def resolve(_client, _peer):
        return 10, TARGET

    class Client:
        async def get_messages(self, entity, *, ids=None, limit=None):
            state.reads.append((entity, ids))
            if limit == 1:
                return [state.outgoing]
            return state.source if ids == 48 else state.outgoing

        async def download_media(self, message, *, file):
            assert message is state.source and file is bytes
            state.downloads += 1
            return CONTENT

        async def send_message(self, *_args, **_kwargs):
            pytest.fail("migration must never send another message")

    def save_ledger(_path, ledger):
        state.ledger = deepcopy(ledger)

    def save_download(content, **_kwargs):
        assert content == CONTENT
        state.saves += 1
        return tmp_path / "synthetic.mp4", hashlib.sha256(content).hexdigest(), None

    monkeypatch.setattr(bridge, "_resolve_peer", resolve)
    monkeypatch.setattr(bridge, "_load_idempotency", lambda _path: deepcopy(state.ledger))
    monkeypatch.setattr(bridge, "_save_idempotency", save_ledger)
    monkeypatch.setattr(bridge, "_save_private_download", save_download)
    monkeypatch.setattr(bridge, "_read_private_download", lambda *_args, **_kwargs: CONTENT)
    state.client = Client()
    state.config = SimpleNamespace(state_dir=tmp_path, socket_path=tmp_path / "bridge.sock")
    state.source = video_message(1.25)
    state.outgoing = Message(
        id=101,
        peer_id=PeerUser(10),
        date=DATE,
        message="Synthetic answer",
        out=True,
        reply_to=MessageReplyHeader(reply_to_msg_id=48),
    )
    yield state
    assert list(tmp_path.iterdir()) == []


def download(runtime, **request):
    return complete(
        bridge._handle_download(runtime.client, runtime.config, {"peer": "10", "message_id": 48, **request})
    )


@pytest.mark.parametrize("duration", [0.000001, 0.25, 0.999, 1.0, 1.25, 45.0, 120.0])
def test_valid_double_duration_survives_dry_run_apply_and_replay(runtime, duration):
    runtime.source = video_message(duration)
    dry = download(runtime)
    assert dry["media"]["duration_seconds"] == duration
    assert type(dry["media"]["duration_seconds"]) is (int if duration.is_integer() else float)
    json.dumps(dry, allow_nan=False)
    request = {"mode": "apply", "idempotency_key": "synthetic-download", "contract_token": dry["contract_token"]}
    applied = download(runtime, **request)
    replayed = download(runtime, **request)
    assert applied["verified"] and replayed["replayed"]
    assert runtime.downloads == runtime.saves == 1


@pytest.mark.parametrize(
    "duration", [0.0, -0.1, 120.000001, 120.5, 121.0, 1.7976931348623157e308, math.nan, math.inf, -math.inf]
)
def test_invalid_duration_is_rejected_before_download_and_never_serializes_nonfinite(runtime, duration):
    runtime.source = video_message(duration)
    metadata = bridge._message_media_metadata(runtime.source)
    assert not metadata["downloadable"]
    json.dumps(metadata, allow_nan=False)
    with pytest.raises(bridge.BridgeError, match=r"^media_not_downloadable$"):
        download(runtime)
    assert runtime.downloads == runtime.saves == 0 and runtime.ledger == {}


@pytest.mark.parametrize(
    "size,allowed", [(0, False), (bridge.MAX_DOWNLOAD_BYTES, True), (bridge.MAX_DOWNLOAD_BYTES + 1, False)]
)
def test_fractional_duration_keeps_byte_limit_without_allocating_the_declared_size(runtime, size, allowed):
    runtime.source = video_message(0.25, size=size)
    assert bridge._message_media_metadata(runtime.source)["downloadable"] is allowed
    if allowed:
        assert download(runtime)["media"]["size_bytes"] == size
    else:
        with pytest.raises(bridge.BridgeError, match=r"^media_not_downloadable$"):
            download(runtime)
    assert runtime.downloads == runtime.saves == 0


@pytest.mark.parametrize("width,height", [(0, 360), (640, 0), (-1, 360)])
def test_fractional_video_keeps_dimension_guard(runtime, width, height):
    runtime.source = video_message(0.25, width=width, height=height)
    with pytest.raises(bridge.BridgeError, match=r"^media_not_downloadable$"):
        download(runtime)
    assert runtime.downloads == 0


@pytest.mark.parametrize("duration", [1, 45, 120])
def test_integral_duration_preserves_legacy_fingerprint_and_download_contract(runtime, duration):
    runtime.source = video_message(duration)
    fingerprint = legacy_fingerprint(duration)
    assert bridge._message_media_metadata(runtime.source)["fingerprint"] == fingerprint
    token = bridge.issue_download_contract(SECRET, peer_id=10, message_id=48, media_fingerprint=fingerprint)
    assert download(runtime, mode="apply", idempotency_key="legacy-key", contract_token=token)["verified"]


def test_fractional_legacy_download_token_and_receipt_cannot_trigger_another_download(runtime):
    old_fingerprint = legacy_fingerprint(1.25)
    old_token = bridge.issue_download_contract(SECRET, peer_id=10, message_id=48, media_fingerprint=old_fingerprint)
    with pytest.raises(bridge.BridgeError, match=r"^contract_media_changed$"):
        download(runtime, mode="apply", idempotency_key="original-key", contract_token=old_token)
    runtime.ledger = {
        "original-key": {
            "operation": "download_media",
            "peer_id": 10,
            "message_id": 48,
            "media_fingerprint": old_fingerprint,
        }
    }
    before = deepcopy(runtime.ledger)
    fresh = download(runtime)
    with pytest.raises(bridge.BridgeError, match=r"^idempotency_key_conflict$"):
        download(runtime, mode="apply", idempotency_key="original-key", contract_token=fresh["contract_token"])
    assert runtime.ledger == before and runtime.downloads == runtime.saves == 0


@pytest.mark.parametrize("duration,allowed", [(1, True), (42, True), (600, True), (0, False), (601, False)])
def test_audio_duration_contract_remains_integer(runtime, duration, allowed):
    document = runtime.source.document
    document.mime_type = "audio/ogg"
    document.attributes = [
        DocumentAttributeAudio(duration=duration, voice=True),
        DocumentAttributeFilename("synthetic.ogg"),
    ]
    metadata = bridge._message_media_metadata(runtime.source)
    assert metadata["downloadable"] is allowed
    assert metadata["duration_seconds"] == (duration or None)
    if duration:
        assert type(metadata["duration_seconds"]) is int
    if allowed:
        assert bridge._require_monitor_voice_metadata(runtime.source) == metadata


@pytest.mark.parametrize("known", [False, True])
def test_fractional_legacy_reply_is_held_and_exact_readback_keeps_original_key(runtime, known):
    source_sha = hashlib.sha256(runtime.source.message.encode()).hexdigest()
    answer_sha = hashlib.sha256(runtime.outgoing.message.encode()).hexdigest()
    old_media = {"version": 1, "fingerprint": legacy_fingerprint(1.25)}
    job = {"job_id": "synthetic-job", "lease": "synthetic-lease", "revision": 1, "state": "needs_review", "peer_id": 10}
    key = "tgjob-" + hashlib.sha256(b"synthetic-job:result:1:0").hexdigest()
    effect = {
        "job_id": job["job_id"],
        "effect_key": key,
        "peer_id": 10,
        "kind": "result",
        "context_revision": 1,
        "effect_slot": 0,
        "attempted": True,
        "verified": False,
        "receipt_key": None,
        "sent_message_id": None,
        "text_sha256": answer_sha,
        "reply_to_message_id": 48,
    }
    old_token = bridge.issue_send_contract(
        SECRET,
        peer_id=10,
        text=runtime.outgoing.message,
        last_message_id=101,
        reply_to_message_id=48,
        reply_message_sha256=source_sha,
        reply_source_media=old_media,
    )

    def send(mode, token=""):
        return complete(
            bridge._handle_send_text_to_entity(
                runtime.client,
                runtime.config,
                {"contract_token": token},
                entity=10,
                target=TARGET,
                text=runtime.outgoing.message,
                mode=mode,
                idempotency_key=key,
                reply_to_message_id=48,
            )
        )

    with pytest.raises(bridge.BridgeError, match=r"^contract_reply_source_changed$"):
        send("apply", old_token)
    runtime.ledger = {
        key: {
            "operation": "send_text",
            "peer_id": 10,
            "message_id": 101 if known else 0,
            "text_sha256": answer_sha,
            "reply_to_message_id": 48,
            "reply_message_sha256": source_sha,
            "reply_source_media": old_media,
            "readback_verified": False,
        }
    }
    before = deepcopy(runtime.ledger)
    fresh = send("dry_run")
    expected = "idempotency_key_conflict" if known else "send_outcome_uncertain"
    with pytest.raises(bridge.BridgeError, match="^" + expected + "$"):
        send("apply", fresh["contract_token"])
    assert runtime.ledger == before

    verified, reconciled = [], []
    monitor = SimpleNamespace(
        inbox=SimpleNamespace(
            read_job=lambda _key: job,
            read_effect=lambda _key: effect,
            verify_effect=lambda **receipt: verified.append(receipt),
        ),
        reconcile_outgoing=reconciled.append,
        changed=lambda: None,
    )
    runtime.reads.clear()
    request = {"job_id": job["job_id"], "lease": job["lease"], "expected_revision": 1, "effect_key": key}
    if not known:
        request.update(message_id=101, effects_reviewed=True)
    result = complete(bridge._handle_job_effect_readback(runtime.client, runtime.config, request, monitor))
    assert result["verified"] and result["receipt_key"] == key
    assert runtime.reads == [(10, 101)] and reconciled == [10]
    assert verified == [{"effect_key": key, "receipt_key": key, "message_id": 101}]
    assert runtime.ledger == {key: before[key] | {"message_id": 101, "readback_verified": True}}
    assert runtime.downloads == runtime.saves == 0
