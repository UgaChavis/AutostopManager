"""Bounded client media/context and cleanup through an isolated native wrapper."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import av
import pytest
from test_telegram_media_native import _jpeg, _media_message, tl
from test_telegram_send_voice import _ogg_audio

from autostop_manager import telegram_bridge as bridge, telegram_monitor_voice as voice, telegram_video_preview as video


@pytest.fixture
def client_context(tmp_path, monkeypatch):
    config = bridge.TelegramConfig(
        123456,
        "a" * 32,
        state_dir=tmp_path / "state",
        session_path=tmp_path / "state" / "account",
        socket_path=tmp_path / "run" / "bridge.sock",
    )
    monitor = bridge.InboundMonitor()
    messages = {
        100: _media_message("photo", _jpeg(), identifier=100, out=False, caption="Фото детали"),
        101: _media_message("voice", _ogg_audio(), identifier=101, out=False, caption=""),
        102: tl.Message(
            id=102, peer_id=tl.PeerUser(42), date=datetime.now(UTC), out=False, message="Передние, две штуки"
        ),
        150: tl.Message(id=150, peer_id=tl.PeerUser(43), date=datetime.now(UTC), out=False, message="OTHER_CLIENT"),
        99: tl.Message(
            id=99, peer_id=tl.PeerUser(42), date=datetime.now(UTC), out=True, message="Ранее согласованные условия"
        ),
    }
    refs = {identifier: monitor.record(messages[identifier]) for identifier in (100, 150, 101, 102)}
    assert monitor.record(messages[99]) is None
    monkeypatch.setattr(bridge, "_owner_peer_id", lambda *_args, **_kwargs: 84)
    monkeypatch.setattr(bridge, "_schedule_monitor_voice_stage_expiry", lambda *_args: None)

    class Client:
        content = _ogg_audio()
        downloads = 0

        async def get_messages(self, entity, *, ids=None, limit=None):
            if isinstance(ids, list):
                return [messages[identifier] for identifier in ids]
            if ids is not None:
                return messages[ids]
            return sorted(
                [message for message in messages.values() if message.chat_id == entity.id],
                key=lambda message: message.id,
                reverse=True,
            )[:limit]

        async def get_entity(self, peer):
            return tl.User(id=int(peer), first_name="Director", bot=False)

        async def download_media(self, message, *, file):
            assert message is messages[101] and file is bytes
            self.downloads += 1
            return self.content

        async def send_message(self, *_args, **_kwargs):
            pytest.fail("A context/media read must never send")

    return config, monitor, Client(), messages, refs


def _operation(runtime, request):
    config, monitor, client, _messages, _refs = runtime
    return asyncio.run(bridge._handle_operation(client, config, request, inbound_monitor=monitor))


def test_client_photo_voice_clarification_keep_exact_context_and_prior_outgoing(client_context):
    _config, monitor, client, _messages, refs = client_context
    target = _operation(client_context, {"operation": "monitor_target", "event_id": refs[102]})
    assert target["role"] == "client" and target["target"]["id"] == 42
    assert target["reply_to_message_id"] == 102
    context = _operation(client_context, {"operation": "monitor_context", "event_id": refs[102], "limit": 5})
    assert [row["event_id"] for row in context["messages"]] == [refs[100], refs[101], refs[102]]
    assert [row["message"]["media_kind"] for row in context["messages"]] == ["photo", "voice", None]
    assert [row["message"]["text"] for row in context["messages"]] == ["Фото детали", "", "Передние, две штуки"]
    assert "OTHER_CLIENT" not in json.dumps(context)
    dialogue = _operation(client_context, {"operation": "read", "peer": "42", "limit": 20})
    assert [row["id"] for row in dialogue["messages"]] == [99, 100, 101, 102]
    assert dialogue["messages"][0]["out"] is True
    assert monitor.status()["open_events"] == 4 and client.downloads == 0


def _isolated_runner(tmp_path: Path, inbox: Path, mode: str) -> Path:
    assert inbox.is_relative_to(tmp_path) and inbox.is_absolute()
    model = tmp_path / "stub-model"
    model.mkdir(mode=0o700)
    runner = tmp_path / "native-media-runner"
    source = Path(__file__).resolve().parents[1]
    script = f"""#!{sys.executable}
import sys
from pathlib import Path
from types import SimpleNamespace
import json
sys.path.insert(0, {str(source)!r})
from autostop_manager import telegram_transcribe as module
INBOX = Path({str(inbox)!r})
MODEL = Path({str(model)!r})
MODE = {mode!r}
assert sys.argv[1] == 'transcribe'
if MODE == 'invalid_utf8':
    sys.stdout.buffer.write(bytes([255]))
    raise SystemExit(0)
if MODE == 'whitespace':
    print(json.dumps({{'ok': True, 'text': '   ', 'language': 'ru', 'cleanup_verified': True}}))
    raise SystemExit(0)
def paths(account):
    assert account == 'work'
    return INBOX, MODEL
class Model:
    def transcribe(self, pcm, **kwargs):
        assert pcm.ndim == 1 and pcm.dtype.name == 'float32' and len(pcm) == 16000
        assert kwargs['condition_on_previous_text'] is False
        if MODE == 'empty':
            segments = []
        elif MODE == 'inference_failure':
            def segments_iterator():
                yield SimpleNamespace(text='PRIVATE_PARTIAL_TRANSCRIPT')
                raise RuntimeError('PRIVATE_BACKEND_PAYLOAD')
            segments = segments_iterator()
        else:
            segments = [SimpleNamespace(text='Synthetic spoken request')]
        return segments, SimpleNamespace(language='ru', language_probability=1.0)
def model_loader(path, **kwargs):
    assert path == MODEL and kwargs['system_owned_model'] is True
    return Model()
module.account_media_paths = paths
module._load_model = model_loader
if MODE == 'runner_cleanup_failure':
    def cleanup(*args, **kwargs):
        raise module.TranscriptionError('audio_cleanup_failed')
    module.discard_private_audio = cleanup
raise SystemExit(module.main(['--account', 'work', *sys.argv[2:]]))
"""
    runner.write_text(script, encoding="utf-8")
    runner.chmod(0o700)
    return runner


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("success", None),
        ("empty", "transcription_empty"),
        ("inference_failure", "transcription_failed"),
        ("runner_cleanup_failure", "audio_cleanup_failed"),
        ("corrupt", "audio_probe_failed"),
        ("invalid_utf8", "monitor_voice_transcription_invalid"),
        ("whitespace", "monitor_voice_transcription_invalid"),
    ],
)
def test_native_monitored_voice_wrapper_cleans_exact_media_without_inventing_text(
    client_context, monkeypatch, tmp_path, mode, expected
):
    config, monitor, client, messages, refs = client_context
    if mode == "corrupt":
        client.content = client.content[:22]
    messages[101] = _media_message("voice", client.content, identifier=101, out=False, caption="")
    inbox = config.socket_path.parent / "inbox"
    runner = _isolated_runner(tmp_path, inbox, mode)
    requests = []

    def request(payload):
        requests.append(payload)
        return _operation(client_context, payload)

    monkeypatch.setattr(voice, "_request", request)
    monkeypatch.setattr(voice, "_media_runner_path", lambda: runner)
    monkeypatch.setattr(voice, "account_inbox_dir", lambda account: inbox if account == "work" else None)
    if expected is None:
        result = voice.transcribe_monitored_voice(refs[101])
        assert result["transcript"] == "Synthetic spoken request" and result["cleanup_verified"] is True
        assert "media_handle" not in result and "saved_path" not in result
    else:
        with pytest.raises(voice.MonitorVoiceError, match=expected) as error:
            voice.transcribe_monitored_voice(refs[101])
        assert error.value.cleanup_verified is True
        assert "PRIVATE" not in str(error.value)
    assert [request["operation"] for request in requests] == ["monitor_voice_stage", "monitor_voice_discard"]
    assert list(inbox.iterdir()) == []
    assert monitor.voice_stage(requests[-1]["media_handle"]) is None
    assert monitor.status()["open_events"] == 4 and client.downloads == 1
    context = _operation(client_context, {"operation": "monitor_context", "event_id": refs[102], "limit": 5})
    assert context["messages"][-1]["message"]["text"] == "Передние, две штуки"


def _mp4(*, codec="mpeg4", width=64, duration_limit=False):
    output = io.BytesIO()
    with av.open(output, "w", format="mp4") as container:
        stream = container.add_stream(codec, rate=8)
        stream.width, stream.height = width, 48
        stream.pix_fmt = "yuvj420p" if codec == "mjpeg" else "yuv420p"
        for index in range(2 if duration_limit else 8):
            frame = av.VideoFrame(width, 48, "rgb24")
            frame.planes[0].update(b"\0" * frame.planes[0].buffer_size)
            frame.pts = index * 121 * 8 if duration_limit else index
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode(None):
            container.mux(packet)
    return output.getvalue()


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (None, None),
        ("truncated", "video_probe_failed"),
        ("codec", "video_streams_invalid"),
        ("duration", "video_duration_invalid"),
        ("dimensions", "video_dimensions_invalid"),
        ("render", "storyboard_render_failed"),
    ],
)
def test_native_video_decode_preview_limits_and_error_cleanup(tmp_path, monkeypatch, capsys, failure, expected):
    inbox = tmp_path / "inbox"
    inbox.mkdir(mode=0o700)
    content = _mp4(
        codec="mjpeg" if failure == "codec" else "mpeg4",
        width=4098 if failure == "dimensions" else 64,
        duration_limit=failure == "duration",
    )
    source = inbox / "clip.mp4"
    source.write_bytes(content[:22] if failure == "truncated" else content)
    source.chmod(0o600)
    monkeypatch.setattr(video, "account_inbox_path", lambda account: inbox if account == "work" else None)
    if failure == "render":
        native_run = video.subprocess.run

        def run(argv, **kwargs):
            if argv[0] == "/usr/bin/ffmpeg":
                assert Path(argv[-1]).parent == inbox
                Path(argv[-1]).write_bytes(b"PRIVATE_PARTIAL_PREVIEW")
                return SimpleNamespace(returncode=1)
            return native_run(argv, **kwargs)

        monkeypatch.setattr(video.subprocess, "run", run)
    result = video.main(["--account", "work", "--file", str(source), "--delete-after"])
    payload = json.loads(capsys.readouterr().out)
    assert not source.exists()
    if expected is None:
        assert result == 0 and payload["ok"] is True and payload["frame_count"] == 8
        preview = Path(payload["preview_path"])
        assert preview.parent == inbox and preview.stat().st_mode & 0o777 == 0o600
        with av.open(preview) as container:
            image = next(container.decode(video=0))
            assert image.width > 0 and image.height > 0
        preview.unlink()
    else:
        assert result == 1 and payload == {"ok": False, "error": expected}
    assert list(inbox.iterdir()) == []
