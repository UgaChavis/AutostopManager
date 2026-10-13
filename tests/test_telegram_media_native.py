"""Real TL metadata and native media handlers, with no Telegram network I/O."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
from types import SimpleNamespace

import av
import pytest
from test_telegram_bridge import _minimal_pdf
from test_telegram_bridge_durable import _job
from test_telegram_send_voice import _ogg_audio

from autostop_manager import telegram_bridge as bridge
from autostop_manager.telegram_inbox import TelegramInbox

tl = pytest.importorskip("telethon.tl.types")


def _jpeg() -> bytes:
    encoder = av.CodecContext.create("mjpeg", "w")
    encoder.width, encoder.height, encoder.pix_fmt = 16, 16, "yuvj420p"
    frame = av.VideoFrame(16, 16, "rgb24")
    frame.planes[0].update(b"\0" * frame.planes[0].buffer_size)
    return b"".join(bytes(packet) for packet in (*encoder.encode(frame), *encoder.encode(None)))


def _media_message(
    kind, content, *, identifier=200, peer=42, out=True, caption="<b>native</b>", reply=0, channel=False
):
    at = datetime.now(UTC)
    if kind == "photo":
        photo = tl.Photo(
            id=identifier,
            access_hash=0,
            file_reference=b"",
            date=at,
            sizes=[tl.PhotoSize("x", 16, 16, len(content))],
            dc_id=1,
        )
        media = tl.MessageMediaPhoto(photo=photo)
    else:
        name, mime = ("media.pdf", "application/pdf") if kind == "document" else ("media.ogg", "audio/ogg")
        attributes = [tl.DocumentAttributeFilename(name)]
        if kind == "voice":
            attributes.append(tl.DocumentAttributeAudio(duration=1, voice=True))
        document = tl.Document(
            id=identifier,
            access_hash=0,
            file_reference=b"",
            date=at,
            mime_type=mime,
            size=len(content),
            dc_id=1,
            attributes=attributes,
        )
        media = tl.MessageMediaDocument(document=document)
    return tl.Message(
        id=identifier,
        peer_id=tl.PeerChannel(peer) if channel else tl.PeerUser(peer),
        date=at,
        out=out,
        message=caption,
        media=media,
        reply_to=tl.MessageReplyHeader(reply_to_msg_id=reply) if reply else None,
    )


@pytest.fixture
def media_case(tmp_path, monkeypatch):
    outbox = tmp_path / "run" / "outbox"
    outbox.mkdir(parents=True, mode=0o700)
    config = bridge.TelegramConfig(
        123456,
        "a" * 32,
        session_path=tmp_path / "state" / "account",
        state_dir=tmp_path / "state",
        socket_path=tmp_path / "run" / "bridge.sock",
    )
    content = {"photo": _jpeg(), "document": _minimal_pdf(), "voice": _ogg_audio()}
    paths = {}
    for kind, suffix in (("photo", ".jpg"), ("document", ".pdf"), ("voice", ".ogg")):
        path = outbox / ("media" + suffix)
        path.write_bytes(content[kind])
        path.chmod(0o600)
        paths[kind] = path

    async def resolve(_client, peer):
        assert peer == "42"
        return 42, {"id": 42, "kind": "private"}

    monkeypatch.setattr(bridge, "_resolve_peer", resolve)

    class Client:
        sent = 0
        downloads = 0
        fault = None
        outgoing = None
        source = tl.Message(id=100, peer_id=tl.PeerUser(42), date=datetime.now(UTC), out=False, message="request")

        async def get_messages(self, _entity, *, ids=None, limit=None):
            if ids is not None:
                return self.outgoing if ids == 200 else self.source
            return ([self.outgoing, self.source] if self.outgoing is not None else [self.source])[:limit]

        async def send_file(self, entity, upload, *, caption, **kwargs):
            assert entity == 42 and kwargs["parse_mode"] is None
            kind = "voice" if kwargs.get("voice_note") else "document" if kwargs["force_document"] else "photo"
            self.uploaded = upload.read()
            assert self.uploaded == content[kind]
            self.sent += 1
            self.outgoing = _media_message(
                kind,
                self.uploaded,
                caption=caption,
                reply=kwargs.get("reply_to", 0),
                identifier=201 if self.fault == "id" else 200,
                peer=43 if self.fault == "chat" else 42,
                out=self.fault != "incoming",
                channel=self.fault == "channel",
            )
            if self.fault == "wrong_photo_type":
                self.outgoing = _media_message("document", self.uploaded, caption=caption)
            return SimpleNamespace(id=200)

        async def download_media(self, _message, *, file):
            assert file is bytes
            self.downloads += 1
            return self.uploaded

    def request(kind):
        return {"operation": "send_" + kind, "peer": "42", kind: str(paths[kind]), "caption": "<b>native</b>"}

    return SimpleNamespace(config=config, client=Client(), content=content, request=request)


def _invoke(case, request, *, monitor=None):
    return asyncio.run(bridge._handle_operation(case.client, case.config, request, inbound_monitor=monitor))


def _apply(case, request, dry, *, monitor=None):
    return _invoke(
        case,
        request
        | {
            "mode": "apply",
            "contract_token": dry["contract_token"],
            "idempotency_key": dry.get("idempotency_key", "native-media"),
        },
        monitor=monitor,
    )


@pytest.mark.parametrize("kind", ["photo", "document", "voice"])
def test_real_tl_media_handler_verifies_and_replays_exact_outgoing(media_case, kind):
    request = media_case.request(kind)
    dry = _invoke(media_case, request)
    assert _apply(media_case, request, dry)["verified"] is True
    assert _apply(media_case, request, dry)["replayed"] is True
    assert media_case.client.sent == 1
    assert media_case.client.downloads == (0 if kind == "photo" else 2)


@pytest.mark.parametrize("kind", ["photo", "document", "voice"])
@pytest.mark.parametrize("fault", ["id", "chat", "incoming", "channel"])
def test_real_tl_media_wrong_identity_cannot_be_verified_or_resent(media_case, kind, fault):
    media_case.client.fault = fault
    request = media_case.request(kind)
    dry = _invoke(media_case, request)
    for _attempt in range(2):
        with pytest.raises(bridge.BridgeError, match="readback_failed"):
            _apply(media_case, request, dry)
    assert media_case.client.sent == 1
    ledger = json.loads((media_case.config.state_dir / "idempotency.json").read_text())
    assert ledger["native-media"]["message_id"] == 200
    assert ledger["native-media"]["readback_verified"] is False


def test_real_tl_document_with_matching_caption_cannot_prove_photo_delivery(media_case):
    media_case.client.fault = "wrong_photo_type"
    request = media_case.request("photo")
    with pytest.raises(bridge.BridgeError, match="send_readback_failed"):
        _apply(media_case, request, _invoke(media_case, request))


@pytest.mark.parametrize("kind", ["photo", "document"])
@pytest.mark.parametrize("fault", ["id", "chat", "incoming", "channel"])
def test_client_job_media_fault_keeps_effect_unverified(media_case, kind, fault):
    inbox = TelegramInbox(media_case.config.state_dir / "inbound.sqlite3")
    try:
        inbox.duty_set(True, at=(datetime.now(UTC) - timedelta(hours=1)).isoformat())
        monitor = bridge.DurableInboundMonitor(inbox, enabled=True)
        monitor.record(media_case.client.source)
        job = _job(monitor)
        media_case.client.fault = fault
        request = media_case.request(kind) | {
            "job_id": job["job_id"],
            "lease": job["lease"],
            "revision": job["revision"],
            "kind": "result",
            "reply_to_message_id": 100,
        }
        dry = _invoke(media_case, request, monitor=monitor)
        with pytest.raises(bridge.BridgeError, match="readback_failed"):
            _apply(media_case, request, dry, monitor=monitor)
        effect = inbox.read_job(job["job_id"])["effects"][0]
        assert effect["verified"] == 0 and effect["attempted"] == 1
        assert inbox.read_job(job["job_id"])["state"] == "running"
        assert media_case.client.sent == 1
    finally:
        inbox.close()


@pytest.mark.parametrize("kind", ["photo", "document", "voice"])
def test_download_native_media_replays_exact_bytes_then_discard_blocks_missing_replay(media_case, kind):
    client = media_case.client
    client.uploaded = media_case.content[kind]
    client.source = _media_message(kind, client.uploaded, identifier=100, out=False)
    request = {"operation": "download", "peer": "42", "message_id": 100}
    dry = _invoke(media_case, request)
    applied = _apply(media_case, request, dry)
    path = Path(applied["saved_path"])
    assert path.read_bytes() == client.uploaded
    assert path.parent == media_case.config.socket_path.parent / "inbox"
    assert path.stat().st_mode & 0o777 == 0o600
    assert _apply(media_case, request, dry)["replayed"] is True
    assert client.downloads == 1 and client.sent == 0
    assert _invoke(media_case, {"operation": "discard_download", "path": str(path)})["removed"] is True
    assert not path.exists()
    with pytest.raises(bridge.BridgeError, match="download_replay_missing"):
        _apply(media_case, request, dry)
    assert client.downloads == 1


@pytest.mark.parametrize("fault", ["id", "chat", "channel"])
def test_download_cannot_fetch_other_message_or_client_media(media_case, fault):
    client = media_case.client
    client.source = _media_message(
        "photo",
        media_case.content["photo"],
        out=False,
        identifier=101 if fault == "id" else 100,
        peer=43 if fault == "chat" else 42,
        channel=fault == "channel",
    )
    with pytest.raises(bridge.BridgeError, match="message_not_found"):
        _invoke(media_case, {"operation": "download", "peer": "42", "message_id": 100})
    assert client.downloads == 0


@pytest.mark.parametrize("peer_kind", ["group", "channel"])
@pytest.mark.parametrize("out", [False, True])
def test_personal_download_retains_exact_group_channel_and_both_directions(media_case, monkeypatch, peer_kind, out):
    client = media_case.client
    client.uploaded = media_case.content["photo"]
    private = _media_message("photo", client.uploaded)
    peer = tl.PeerChat(42) if peer_kind == "group" else tl.PeerChannel(42)
    client.source = tl.Message(
        id=100,
        peer_id=peer,
        date=datetime.now(UTC),
        out=out,
        message="group media",
        media=private.media,
    )

    async def resolve(_client, requested):
        assert requested == str(client.source.chat_id)
        return client.source.chat_id, {"id": client.source.chat_id, "kind": peer_kind}

    monkeypatch.setattr(bridge, "_resolve_peer", resolve)
    request = {"operation": "download", "peer": str(client.source.chat_id), "message_id": 100}
    dry = _invoke(media_case, request)
    applied = _apply(media_case, request, dry)
    assert applied["verified"] is True and client.downloads == 1
    assert applied["target"]["id"] == client.source.chat_id


@pytest.mark.parametrize("failure", ["size", "signature", "unsupported"])
def test_download_invalid_media_never_creates_private_file_or_claims_content(media_case, failure):
    client = media_case.client
    client.uploaded = media_case.content["photo"]
    client.source = _media_message("photo", client.uploaded, identifier=100, out=False)
    if failure == "unsupported":
        client.source.media = tl.MessageMediaUnsupported()
    elif failure == "size":
        client.uploaded = client.uploaded[:-1]
    else:
        client.uploaded = b"x" * len(client.uploaded)
    request = {"operation": "download", "peer": "42", "message_id": 100}
    if failure == "unsupported":
        with pytest.raises(bridge.BridgeError, match="media_not_downloadable"):
            _invoke(media_case, request)
        assert client.downloads == 0
    else:
        dry = _invoke(media_case, request)
        with pytest.raises(bridge.BridgeError, match=r"download_size_mismatch|download_content_invalid"):
            _apply(media_case, request, dry)
    assert not (media_case.config.socket_path.parent / "inbox").exists()
    assert not (media_case.config.state_dir / "idempotency.json").exists()
