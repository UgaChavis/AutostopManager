from __future__ import annotations

import asyncio
from dataclasses import replace
import hashlib
import io
import json
import os
from types import SimpleNamespace

import av
import pytest

from autostop_manager import telegram_bridge as bridge


def _ogg_audio(*, codec="libopus", samples=48000) -> bytes:
    output = io.BytesIO()
    with av.open(output, "w", format="ogg") as container:
        stream = container.add_stream(codec, rate=48000)
        layout = "stereo" if codec == "vorbis" else "mono"
        stream.layout = layout
        if codec == "vorbis":
            stream.options = {"strict": "-2"}
        frame = av.AudioFrame(format="s16", layout=layout, samples=samples)
        frame.sample_rate = 48000
        frame.planes[0].update(b"\0" * (samples * 2 * (2 if layout == "stereo" else 1)))
        for packet in stream.encode(frame):
            container.mux(packet)
        for packet in stream.encode(None):
            container.mux(packet)
    return output.getvalue()


@pytest.fixture(scope="module")
def voice_bytes():
    return _ogg_audio()


class DocumentAttributeAudio:
    def __init__(self, duration, voice=True):
        self.duration = duration
        self.voice = voice


@pytest.fixture
def scenario(tmp_path, monkeypatch, voice_bytes):
    runtime = tmp_path / "run"
    outbox = runtime / "outbox"
    outbox.mkdir(parents=True, mode=0o700)
    voice = outbox / "test.ogg"
    voice.write_bytes(voice_bytes)
    voice.chmod(0o600)
    config = bridge.TelegramConfig(
        api_id=123456,
        api_hash="0" * 32,
        session_path=tmp_path / "state" / "account",
        state_dir=tmp_path / "state",
        socket_path=runtime / "bridge.sock",
        owner_config_path=None,
    )

    async def resolve(_client, peer):
        return object(), {"id": int(peer), "kind": "private"}

    async def last(client, _entity):
        return client.last_id

    monkeypatch.setattr(bridge, "_resolve_peer", resolve)
    monkeypatch.setattr(bridge, "_last_message_id", last)
    monkeypatch.setattr(bridge, "_voice_attribute", DocumentAttributeAudio)

    class Client:
        last_id = 20
        sends = 0
        downloads = 0
        uploaded = b""
        readback = None
        uncertain = False

        async def send_file(self, _entity, upload, **kwargs):
            assert kwargs == {
                "caption": "<b>тест</b>",
                "force_document": False,
                "voice_note": True,
                "mime_type": "audio/ogg",
                "attributes": kwargs["attributes"],
                "parse_mode": None,
            }
            attribute = kwargs["attributes"][0]
            assert attribute.voice is True and attribute.duration == 1
            assert upload.name == voice.name
            ledger = json.loads((config.state_dir / "idempotency.json").read_text())
            assert ledger["voice-effect"]["message_id"] == 0
            assert ledger["voice-effect"]["voice_sha256"] == hashlib.sha256(voice_bytes).hexdigest()
            voice.write_bytes(b"the path was replaced during send")
            self.uploaded = upload.read()
            self.sends += 1
            self.last_id = 30
            if self.uncertain:
                raise TimeoutError("response lost after Telegram acceptance")
            return SimpleNamespace(id=30)

        async def get_messages(self, _entity, *, ids):
            assert ids == 30
            if self.readback is not None:
                return self.readback
            return self.message()

        def message(self):
            return SimpleNamespace(
                id=30,
                out=True,
                message="<b>тест</b>",
                media=object(),
                file=SimpleNamespace(name=voice.name, mime_type="audio/ogg", size=len(voice_bytes)),
                document=SimpleNamespace(attributes=[DocumentAttributeAudio(1)]),
            )

        async def download_media(self, _message, *, file):
            assert file is bytes
            self.downloads += 1
            return self.uploaded

    client = Client()
    request = {"operation": "send_voice", "peer": "10", "voice": str(voice), "caption": "<b>тест</b>"}
    return SimpleNamespace(config=config, outbox=outbox, voice=voice, client=client, request=request)


def _dry(scenario):
    return asyncio.run(bridge._handle_operation(scenario.client, scenario.config, scenario.request))


def _apply(scenario, dry):
    return asyncio.run(
        bridge._handle_operation(
            scenario.client,
            scenario.config,
            scenario.request
            | {"mode": "apply", "contract_token": dry["contract_token"], "idempotency_key": "voice-effect"},
        )
    )


def test_voice_upload_is_real_audio_immutable_and_replayed_with_independent_readback(scenario, voice_bytes):
    dry = _dry(scenario)
    assert dry["voice"] is True and dry["mime_type"] == "audio/ogg"
    assert dry["duration_seconds"] == 1
    assert scenario.client.sends == 0
    result = _apply(scenario, dry)
    assert result["verified"] is True and result["replayed"] is False
    assert scenario.client.uploaded == voice_bytes
    scenario.voice.write_bytes(voice_bytes)
    replay = _apply(scenario, dry)
    assert replay["replayed"] is True and replay["verified"] is True
    assert scenario.client.sends == 1 and scenario.client.downloads == 2


def test_voice_actual_telethon_serializes_voice_without_forcing_ordinary_file(scenario, monkeypatch, voice_bytes):
    pytest.importorskip("telethon")
    from telethon.client.uploads import UploadMethods
    from telethon.extensions import BinaryReader
    from telethon.tl import functions, types

    monkeypatch.setattr(bridge, "_voice_attribute", lambda duration: types.DocumentAttributeAudio(duration, voice=True))

    class OfflineClient(UploadMethods):
        last_id = 20
        request = None
        uploaded = b""

        async def get_input_entity(self, _entity):
            return types.InputPeerUser(10, 0)

        async def _parse_message_text(self, caption, parse_mode):
            assert parse_mode is None
            return caption, []

        def build_reply_markup(self, buttons):
            assert buttons is None

        async def upload_file(self, upload, **_kwargs):
            self.uploaded = upload.read()
            assert self.uploaded == voice_bytes
            return types.InputFile(1, 1, upload.name, "0" * 32)

        async def __call__(self, request):
            # Intercept the final RPC boundary. Serialize and parse the actual
            # Telethon object so this covers the TL force_file flag, not only
            # send_file kwargs or get_attributes output.
            assert isinstance(request, functions.messages.SendMediaRequest)
            with BinaryReader(bytes(request)) as reader:
                self.request = reader.tgread_object()
            media = self.request.media
            assert isinstance(media, types.InputMediaUploadedDocument)
            assert media.force_file is False
            assert media.mime_type == "audio/ogg"
            audio = [item for item in media.attributes if isinstance(item, types.DocumentAttributeAudio)]
            assert len(audio) == 1 and audio[0].voice is True and audio[0].duration == 1
            self.last_id = 30
            return SimpleNamespace(id=30)

        def _get_response_message(self, _request, result, _entity):
            return result

        async def get_messages(self, _entity, *, ids):
            assert ids == 30
            message = scenario.client.message()
            message.document.attributes = self.request.media.attributes
            return message

        async def download_media(self, _message, *, file):
            assert file is bytes
            return self.uploaded

    original = scenario.client
    client = OfflineClient()
    dry = asyncio.run(bridge._handle_operation(client, scenario.config, scenario.request))
    result = asyncio.run(
        bridge._handle_operation(
            client,
            scenario.config,
            scenario.request
            | {"mode": "apply", "contract_token": dry["contract_token"], "idempotency_key": "voice-effect"},
        )
    )
    assert result["verified"] is True
    assert original.sends == 0


def test_voice_unknown_network_outcome_blocks_fresh_contract_retry(scenario, voice_bytes):
    dry = _dry(scenario)
    scenario.client.uncertain = True
    with pytest.raises(TimeoutError):
        _apply(scenario, dry)
    scenario.voice.write_bytes(voice_bytes)
    fresh = _dry(scenario)
    with pytest.raises(bridge.BridgeError, match="send_voice_outcome_uncertain"):
        _apply(scenario, fresh)
    assert scenario.client.sends == 1


@pytest.mark.parametrize("change", ["conversation", "file", "caption", "target"])
def test_voice_dry_run_cannot_authorize_changed_effect(scenario, change):
    dry = _dry(scenario)
    if change == "conversation":
        scenario.client.last_id += 1
    elif change == "file":
        scenario.voice.write_bytes(_ogg_audio(samples=96000))
    else:
        scenario.request["caption" if change == "caption" else "peer"] = "changed" if change == "caption" else "11"
    with pytest.raises(bridge.BridgeError, match=r"conversation_changed_since_dry_run|contract_.*_changed"):
        _apply(scenario, dry)
    assert scenario.client.sends == 0


@pytest.mark.parametrize("field", ["voice", "mime", "duration", "size", "caption", "id", "out", "media", "bytes"])
def test_voice_readback_rejects_wrong_type_metadata_or_bytes(scenario, field):
    dry = _dry(scenario)
    message = scenario.client.message()
    if field == "voice":
        message.document.attributes[0].voice = False
    elif field == "mime":
        message.file.mime_type = "application/pdf"
    elif field == "duration":
        message.document.attributes[0].duration += 1
    elif field == "size":
        message.file.size += 1
    elif field == "caption":
        message.message = "wrong"
    elif field == "id":
        message.id += 1
    elif field == "out":
        message.out = False
    elif field == "media":
        message.media = None
    else:

        async def wrong_bytes(_message, *, file):
            return b"x" * len(scenario.client.uploaded)

        scenario.client.download_media = wrong_bytes
    scenario.client.readback = message
    with pytest.raises(bridge.BridgeError, match="send_voice_readback_failed"):
        _apply(scenario, dry)
    assert scenario.client.sends == 1
    ledger = json.loads((scenario.config.state_dir / "idempotency.json").read_text())
    assert ledger["voice-effect"]["message_id"] == 30
    assert ledger["voice-effect"]["readback_verified"] is False


def test_voice_idempotency_key_cannot_rebind_after_success(scenario, voice_bytes):
    _apply(scenario, _dry(scenario))
    scenario.voice.write_bytes(voice_bytes)
    scenario.request["caption"] = "another effect"
    with pytest.raises(bridge.BridgeError, match="idempotency_key_conflict"):
        _apply(scenario, _dry(scenario))
    assert scenario.client.sends == 1


@pytest.mark.parametrize("work_scope", ["owner", "socket", "monitor"])
def test_voice_is_personal_only_and_cannot_bypass_work_job_effects(scenario, work_scope):
    config = scenario.config
    kwargs = {}
    if work_scope == "owner":
        config = replace(config, owner_config_path=scenario.outbox / "owner.json")
    elif work_scope == "socket":
        config = replace(config, socket_path=bridge.WORK_SOCKET_PATH)
    else:
        kwargs["inbound_monitor"] = bridge.InboundMonitor()
    with pytest.raises(bridge.BridgeError, match="personal_account_required"):
        asyncio.run(bridge._handle_operation(scenario.client, config, scenario.request, **kwargs))
    assert scenario.client.sends == 0


@pytest.mark.parametrize("unsafe", ["outside", "symlink", "hardlink", "fifo", "file-mode", "outbox-mode", "oversize"])
def test_voice_file_safety(scenario, voice_bytes, unsafe, monkeypatch):
    path = scenario.voice
    if unsafe == "outside":
        path = scenario.outbox.parent / "outside.ogg"
    elif unsafe == "symlink":
        path = scenario.outbox / "linked.ogg"
        path.symlink_to(scenario.voice.name)
    elif unsafe == "hardlink":
        path = scenario.outbox / "linked.ogg"
        os.link(scenario.voice, path)
    elif unsafe == "fifo":
        path = scenario.outbox / "pipe.ogg"
        os.mkfifo(path, mode=0o600)
    elif unsafe == "file-mode":
        path.chmod(0o640)
    elif unsafe == "outbox-mode":
        scenario.outbox.chmod(0o750)
    else:
        monkeypatch.setattr(bridge, "MAX_VOICE_BYTES", len(voice_bytes) - 1)
    with pytest.raises(bridge.BridgeError, match="voice_"):
        bridge._load_validated_voice_file(path, outbox_dir=scenario.outbox)


def test_voice_rejects_forged_container_wrong_codec_and_excessive_duration(scenario, monkeypatch):
    for content in (b"%PDF-not-a-voice", b"OggSfake", _ogg_audio(codec="vorbis")):
        scenario.voice.write_bytes(content)
        with pytest.raises(bridge.BridgeError, match=r"voice_invalid|voice_codec_invalid"):
            bridge._load_validated_voice_file(scenario.voice, outbox_dir=scenario.outbox)
    scenario.voice.write_bytes(_ogg_audio(samples=96000))
    monkeypatch.setattr(bridge, "MAX_VOICE_DURATION_SECONDS", 1)
    with pytest.raises(bridge.BridgeError, match="voice_duration_invalid"):
        bridge._load_validated_voice_file(scenario.voice, outbox_dir=scenario.outbox)


@pytest.mark.parametrize(
    "field", ["peer_id", "caption", "voice_sha256", "voice_bytes", "voice_name", "duration_seconds"]
)
def test_voice_contract_binds_exact_effect_and_expires(field):
    args = {
        "peer_id": 10,
        "caption": "",
        "voice_sha256": "a" * 64,
        "voice_bytes": 123,
        "voice_name": "note.ogg",
        "duration_seconds": 1,
    }
    token = bridge.issue_voice_contract(b"secret", **args, last_message_id=20, now=1000)
    assert bridge.verify_voice_contract(token, b"secret", **args, now=1000)["last_message_id"] == 20
    wrong = args | {field: args[field] + 1 if isinstance(args[field], int) else args[field] + "changed"}
    with pytest.raises(bridge.BridgeError, match=r"contract_.*_changed"):
        bridge.verify_voice_contract(token, b"secret", **wrong, now=1000)
    with pytest.raises(bridge.BridgeError, match="contract_expired"):
        bridge.verify_voice_contract(token, b"secret", **args, now=1000 + bridge.CONTRACT_TTL_SECONDS + 1)


@pytest.mark.parametrize("peer", ["", "@target", "me", "-10", "0", "+10"])
def test_voice_request_requires_exact_positive_peer(peer):
    with pytest.raises(bridge.BridgeError, match="exact_private_peer_required"):
        bridge.validate_voice_request({"peer": peer, "voice": "/unused.ogg"})


def test_voice_request_requires_apply_key_and_rejects_unsupported_fields():
    base = {"peer": "10", "voice": "/unused.ogg"}
    assert bridge.validate_voice_request(base)[2] == ""
    for updates, error in (
        ({"mode": "apply"}, "idempotency_key_required"),
        ({"mode": "unexpected"}, "mode_invalid"),
        ({"caption": "x" * 1025}, "caption_length_invalid"),
        ({"job_id": "bypass"}, "voice_request_invalid"),
    ):
        with pytest.raises(bridge.BridgeError, match=error):
            bridge.validate_voice_request(base | updates)


def test_voice_cli_uses_guarded_personal_request_and_work_never_connects(monkeypatch, tmp_path, capsys):
    calls = []

    def send_local(socket, request):
        assert socket == tmp_path / "personal.sock"
        calls.append(request)
        return {"ok": True}

    monkeypatch.setattr(bridge, "_socket_from_args", lambda _args: tmp_path / "personal.sock")
    monkeypatch.setattr(bridge, "send_local_request", send_local)
    path = tmp_path / "note.ogg"
    args = ["send-voice", "--peer", "10", "--file", str(path)]
    assert bridge.main(["--account", "personal", *args]) == 0
    assert calls[0] == {
        "operation": "send_voice",
        "peer": "10",
        "voice": str(path),
        "caption": "",
        "mode": "dry_run",
        "contract_token": "",
        "idempotency_key": "",
    }
    assert bridge.main(["--account", "work", *args]) == 1
    assert len(calls) == 1
    assert "personal_account_required" in capsys.readouterr().out
    assert bridge._requires_mutation_lock({"operation": "send_voice", "mode": "apply"}) is True
