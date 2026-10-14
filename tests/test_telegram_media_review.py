from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from autostop_manager import telegram_monitor_voice as voice, telegram_transcribe as transcription
from autostop_manager import telegram_video_preview as video


@pytest.mark.parametrize(
    "character,count,expected_error",
    [
        ("x", 32768, None),
        ("\u044f", 32768, None),
        ("\U0001f600", 32768, None),
        ("\x00", 32768, None),
        ("x", 32769, "transcription_too_large"),
    ],
)
def test_native_transcript_character_limit_survives_monitor_wire(
    monkeypatch, tmp_path, character, count, expected_error
):
    inbox = tmp_path / "inbox"
    inbox.mkdir(mode=0o700)
    path = inbox / "synthetic.ogg"
    path.write_bytes(b"OggSsynthetic")
    path.chmod(0o600)

    class Model:
        def transcribe(self, _pcm, **kwargs):
            assert kwargs["vad_filter"] is True and kwargs["condition_on_previous_text"] is False
            return iter([SimpleNamespace(text=character * count)]), SimpleNamespace(
                language="ru", language_probability=1.0
            )

    monkeypatch.setattr(transcription, "account_media_paths", lambda _account: (inbox, tmp_path / "no-model"))
    monkeypatch.setattr(transcription, "_probe_audio", lambda _path: {"codec": "opus", "duration_seconds": 4.2})
    monkeypatch.setattr(transcription, "_decode_private_audio", lambda _path: object())
    monkeypatch.setattr(transcription, "_load_model", lambda _path, **_kwargs: Model())
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        returncode = transcription.main(["--account", "work", "--file", str(path), "--delete-after"])
    serialized = stdout.getvalue()
    payload = json.loads(serialized)
    assert payload["cleanup_verified"] is True and not path.exists()
    monkeypatch.setattr(voice, "_media_runner_path", lambda: tmp_path / "inert-runner")
    monkeypatch.setattr(
        voice.subprocess, "run", lambda *_args, **_kwargs: SimpleNamespace(returncode=returncode, stdout=serialized)
    )
    if expected_error is not None:
        assert returncode == 1 and payload["error"] == expected_error
        with pytest.raises(voice.MonitorVoiceError, match="^" + expected_error + "$") as raised:
            voice._run_transcriber(path, language="ru")
        assert raised.value.cleanup_verified is True
    else:
        assert returncode == 0 and payload["ok"] is True
        result = voice._run_transcriber(path, language="ru")
        assert result == {"transcript": character * count, "language": "ru", "cleanup_verified": True}


def test_monitor_wire_still_rejects_an_over_budget_json_response(monkeypatch, tmp_path):
    serialized = json.dumps({"ok": True, "text": "synthetic", "language": "ru", "cleanup_verified": True})
    serialized += " " * (voice._MAX_RUNNER_OUTPUT_BYTES + 1)
    monkeypatch.setattr(voice, "_media_runner_path", lambda: tmp_path / "inert-runner")
    monkeypatch.setattr(
        voice.subprocess, "run", lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout=serialized)
    )
    with pytest.raises(voice.MonitorVoiceError, match=r"^monitor_voice_transcription_invalid$"):
        voice._run_transcriber(tmp_path / "synthetic.ogg", language="ru")


@pytest.mark.parametrize(
    "text,language",
    [
        (True, "ru"),
        ("x" * 32769, "ru"),
        ("synthetic", True),
        ("   ", "ru"),
    ],
)
def test_monitor_wire_preserves_transcript_type_and_character_guards(monkeypatch, tmp_path, text, language):
    serialized = json.dumps({"ok": True, "text": text, "language": language, "cleanup_verified": True})
    monkeypatch.setattr(voice, "_media_runner_path", lambda: tmp_path / "inert-runner")
    monkeypatch.setattr(
        voice.subprocess, "run", lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout=serialized)
    )
    with pytest.raises(voice.MonitorVoiceError, match=r"^monitor_voice_transcription_invalid$"):
        voice._run_transcriber(tmp_path / "synthetic.ogg", language="ru")


@pytest.mark.parametrize(
    "duration,expected_error",
    [
        ("1.25", None),
        ("1.234567", None),
        ("0.001", None),
        ("0.0004", None),
        ("0.000001", None),
        ("120", None),
        ("0", "video_duration_invalid"),
        ("121", "video_duration_invalid"),
        ("-0.1", "video_duration_invalid"),
        ("nan", "video_duration_invalid"),
        ("inf", "video_duration_invalid"),
        ("-inf", "video_duration_invalid"),
        ("N/A", "video_probe_invalid"),
    ],
)
def test_video_render_uses_positive_source_duration_and_keeps_public_rounding(
    monkeypatch, tmp_path, duration, expected_error
):
    inbox = tmp_path / "inbox"
    inbox.mkdir(mode=0o700)
    path = inbox / "synthetic.mp4"
    path.write_bytes(b"\x00\x00\x00\x18ftypisomsynthetic")
    path.chmod(0o600)
    calls = []

    def run(argv, **kwargs):
        assert argv[0] in ("/usr/bin/ffprobe", "/usr/bin/ffmpeg") and str(path) in argv
        calls.append(argv[0])
        if argv[0] == "/usr/bin/ffprobe":
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    {
                        "streams": [{"codec_type": "video", "codec_name": "h264", "width": 16, "height": 16}],
                        "format": {"duration": duration},
                    }
                ),
            )
        rendered_duration = argv[argv.index("-t") + 1]
        assert float(rendered_duration) > 0 and float(rendered_duration) == float(duration)
        output = Path(argv[-1])
        assert output.parent == inbox
        output.write_bytes(b"\xff\xd8\xffsynthetic-preview\xff\xd9")
        output.chmod(0o600)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(video, "account_inbox_path", lambda _account: inbox)
    monkeypatch.setattr(video.subprocess, "run", run)
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        returncode = video.main(["--account", "work", "--file", str(path), "--delete-after"])
    payload = json.loads(stdout.getvalue())
    assert not path.exists()
    if expected_error is not None:
        assert returncode == 1 and payload == {"ok": False, "error": expected_error}
        assert "/usr/bin/ffmpeg" not in calls and list(inbox.iterdir()) == []
    else:
        assert returncode == 0 and payload["ok"] is True
        assert payload["duration_seconds"] == round(float(duration), 3)
        assert payload["frame_count"] == 8 and "/usr/bin/ffmpeg" in calls
        preview = Path(payload["preview_path"])
        assert preview.parent == inbox and preview.stat().st_mode & 0o777 == 0o600
        preview.unlink()
        assert list(inbox.iterdir()) == []
