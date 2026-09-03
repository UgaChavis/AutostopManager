from __future__ import annotations

import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from autostop_manager import telegram_transcribe
from autostop_manager.telegram_transcribe import TranscriptionError, transcribe_private_audio


def _private_audio(tmp_path: Path, content: bytes = b"OggSvoice") -> tuple[Path, Path]:
    inbox = tmp_path / "inbox"
    inbox.mkdir(mode=0o700, parents=True)
    audio = inbox / "voice.ogg"
    audio.write_bytes(content)
    audio.chmod(0o600)
    return inbox, audio


def test_transcribe_private_audio_uses_local_model_and_returns_compact_text(monkeypatch, tmp_path) -> None:
    inbox, audio = _private_audio(tmp_path)
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    calls: dict[str, object] = {}

    class Model:
        def transcribe(self, path, **kwargs):
            calls["path"] = path
            calls["kwargs"] = kwargs
            return iter([SimpleNamespace(text=" Привет "), SimpleNamespace(text="из сервиса")]), SimpleNamespace(
                language="ru", language_probability=0.97
            )

    monkeypatch.setattr(
        telegram_transcribe,
        "_probe_audio",
        lambda _path: {"codec": "opus", "duration_seconds": 4.2},
    )
    decoded_audio = object()
    monkeypatch.setattr(telegram_transcribe, "_decode_private_audio", lambda _path: decoded_audio)
    monkeypatch.setattr(
        telegram_transcribe,
        "_load_model",
        lambda _path, cpu_threads, system_owned_model=False: Model(),
    )

    result = transcribe_private_audio(audio, inbox_dir=inbox, model_dir=model_dir)

    assert result["text"] == "Привет из сервиса"
    assert result["language"] == "ru"
    assert result["duration_seconds"] == 4.2
    assert calls["path"] is decoded_audio
    assert calls["kwargs"] == {
        "language": "ru",
        "beam_size": 5,
        "vad_filter": True,
        "condition_on_previous_text": False,
    }


def test_private_audio_rejects_symlink_and_open_permissions(tmp_path) -> None:
    inbox, audio = _private_audio(tmp_path)
    link = inbox / "link.ogg"
    link.symlink_to(audio.name)

    with pytest.raises(TranscriptionError, match="audio_unavailable"):
        transcribe_private_audio(link, inbox_dir=inbox, model_dir=tmp_path / "model")

    audio.chmod(0o640)
    with pytest.raises(TranscriptionError, match="audio_permissions_or_size_invalid"):
        transcribe_private_audio(audio, inbox_dir=inbox, model_dir=tmp_path / "model")


def test_probe_rejects_video_or_excessive_duration(monkeypatch, tmp_path) -> None:
    _inbox, audio = _private_audio(tmp_path)

    def completed(payload):
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload))

    monkeypatch.setattr(
        telegram_transcribe.subprocess,
        "run",
        lambda *_args, **_kwargs: completed(
            {
                "streams": [{"codec_type": "audio", "codec_name": "opus"}, {"codec_type": "video"}],
                "format": {"duration": "5"},
            }
        ),
    )
    with pytest.raises(TranscriptionError, match="audio_streams_invalid"):
        telegram_transcribe._probe_audio(audio)

    monkeypatch.setattr(
        telegram_transcribe.subprocess,
        "run",
        lambda *_args, **_kwargs: completed(
            {"streams": [{"codec_type": "audio", "codec_name": "opus"}], "format": {"duration": "601"}}
        ),
    )
    with pytest.raises(TranscriptionError, match="audio_duration_invalid"):
        telegram_transcribe._probe_audio(audio)


def test_audio_probe_and_decode_allow_only_local_protocols(monkeypatch, tmp_path) -> None:
    _inbox, audio = _private_audio(tmp_path)
    calls: list[tuple[list[str], dict[str, object]]] = []

    class Samples:
        def astype(self, _dtype):
            return self

        def __truediv__(self, _divisor):
            return self

        def tolist(self):
            return [-1.0, 32767 / 32768]

    class Numpy:
        @staticmethod
        def frombuffer(_payload, *, dtype):
            assert dtype == "<i2"
            return Samples()

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        if argv[0] == "/usr/bin/ffprobe":
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    {"streams": [{"codec_type": "audio", "codec_name": "opus"}], "format": {"duration": "2"}}
                ),
            )
        return SimpleNamespace(returncode=0, stdout=b"\x00\x80\xff\x7f")

    monkeypatch.setattr(telegram_transcribe.subprocess, "run", run)
    monkeypatch.setitem(sys.modules, "numpy", Numpy)

    assert telegram_transcribe._probe_audio(audio)["codec"] == "opus"
    decoded = telegram_transcribe._decode_private_audio(audio)

    assert decoded.tolist() == [-1.0, 32767 / 32768]
    assert all("file,pipe" in argv for argv, _kwargs in calls)
    assert calls[0][1]["stdout"] is telegram_transcribe.subprocess.PIPE
    assert calls[0][1]["stderr"] is telegram_transcribe.subprocess.DEVNULL
    assert calls[1][0][0] == "/usr/bin/ffmpeg"
    assert calls[1][0][calls[1][0].index("-t") + 1] == str(telegram_transcribe.MAX_AUDIO_DURATION_SECONDS)
    assert calls[1][1]["stdout"] is telegram_transcribe.subprocess.PIPE
    assert calls[1][1]["stderr"] is telegram_transcribe.subprocess.DEVNULL
    assert calls[1][1]["timeout"] == 40


def test_cli_delete_after_removes_exact_private_audio(monkeypatch, tmp_path, capsys) -> None:
    inbox, audio = _private_audio(tmp_path)
    monkeypatch.setattr(
        telegram_transcribe,
        "account_media_paths",
        lambda _account: (inbox, tmp_path / "work-model"),
    )
    monkeypatch.setattr(
        telegram_transcribe,
        "transcribe_private_audio",
        lambda *_args, **_kwargs: {"ok": True, "text": "готово"},
    )

    assert telegram_transcribe.main(["--account", "work", "--file", str(audio), "--delete-after"]) == 0
    assert not audio.exists()
    assert json.loads(capsys.readouterr().out)["text"] == "готово"


def test_cli_delete_after_reports_cleanup_failure(monkeypatch, tmp_path, capsys) -> None:
    inbox, audio = _private_audio(tmp_path)
    monkeypatch.setattr(
        telegram_transcribe,
        "account_media_paths",
        lambda _account: (inbox, tmp_path / "work-model"),
    )
    monkeypatch.setattr(
        telegram_transcribe,
        "transcribe_private_audio",
        lambda *_args, **_kwargs: {"ok": True, "text": "private transcript"},
    )
    monkeypatch.setattr(
        telegram_transcribe,
        "discard_private_audio",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(TranscriptionError("audio_cleanup_failed")),
    )

    assert telegram_transcribe.main(["--account", "work", "--file", str(audio), "--delete-after"]) == 1
    assert json.loads(capsys.readouterr().out) == {"ok": False, "error": "audio_cleanup_failed"}


def test_cli_delete_after_preserves_processing_error_after_removing_valid_audio(monkeypatch, tmp_path, capsys) -> None:
    inbox, audio = _private_audio(tmp_path)
    monkeypatch.setattr(
        telegram_transcribe,
        "account_media_paths",
        lambda _account: (inbox, tmp_path / "work-model"),
    )
    monkeypatch.setattr(
        telegram_transcribe,
        "transcribe_private_audio",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(TranscriptionError("transcription_failed")),
    )

    assert telegram_transcribe.main(["--account", "work", "--file", str(audio), "--delete-after"]) == 1
    assert not audio.exists()
    assert json.loads(capsys.readouterr().out) == {"ok": False, "error": "transcription_failed"}


def test_cli_delete_after_keeps_processing_error_when_audio_cleanup_fails(monkeypatch, tmp_path, capsys) -> None:
    inbox, audio = _private_audio(tmp_path)
    monkeypatch.setattr(
        telegram_transcribe,
        "account_media_paths",
        lambda _account: (inbox, tmp_path / "work-model"),
    )
    monkeypatch.setattr(
        telegram_transcribe,
        "transcribe_private_audio",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(TranscriptionError("transcription_failed")),
    )
    monkeypatch.setattr(
        telegram_transcribe,
        "discard_private_audio",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(TranscriptionError("audio_cleanup_failed")),
    )

    assert telegram_transcribe.main(["--account", "work", "--file", str(audio), "--delete-after"]) == 1
    assert json.loads(capsys.readouterr().out) == {
        "ok": False,
        "error": "transcription_failed",
        "cleanup_failed": True,
    }


def test_model_loader_fails_closed_without_local_model(tmp_path) -> None:
    with pytest.raises(TranscriptionError, match="transcription_model_unavailable"):
        telegram_transcribe._load_model(tmp_path / "missing", cpu_threads=2)


def test_model_validation_requires_all_pinned_payloads(tmp_path) -> None:
    model_dir = tmp_path / "model"
    model_dir.mkdir(mode=0o700)
    for name in ("config.json", "model.bin", "tokenizer.json"):
        candidate = model_dir / name
        candidate.write_bytes(b"model")
        candidate.chmod(0o600)

    with pytest.raises(TranscriptionError, match="transcription_model_invalid"):
        telegram_transcribe._validate_local_model(model_dir)


def test_system_owned_model_requires_read_only_root_owned_files(tmp_path) -> None:
    if os.geteuid() != 0:
        pytest.skip("requires a root-owned temporary model fixture")
    model_dir = tmp_path / "model"
    model_dir.mkdir(mode=0o750)
    required_files = (
        "config.json",
        "model.bin",
        "tokenizer.json",
        "vocabulary.txt",
    )
    for name in required_files:
        candidate = model_dir / name
        candidate.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
        candidate.write_bytes(b"model")
        candidate.chmod(0o640)

    telegram_transcribe._validate_local_model(model_dir, system_owned_model=True)

    (model_dir / "model.bin").chmod(0o600)
    with pytest.raises(TranscriptionError, match="transcription_model_invalid"):
        telegram_transcribe._validate_local_model(model_dir, system_owned_model=True)


def test_cli_requires_account_and_rejects_manual_model_path() -> None:
    parser = telegram_transcribe.build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(["--file", "/run/example.ogg"])
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "--account",
                "work",
                "--file",
                "/run/example.ogg",
                "--model-dir",
                "/tmp/model",
            ]
        )


def test_cli_work_uses_only_its_fixed_media_paths(monkeypatch, tmp_path, capsys) -> None:
    work_inbox, audio = _private_audio(tmp_path / "work")
    work_model = tmp_path / "work-model"
    calls: dict[str, object] = {}
    monkeypatch.setattr(
        telegram_transcribe,
        "account_media_paths",
        lambda account: (work_inbox, work_model) if account == "work" else pytest.fail("wrong account"),
    )
    monkeypatch.setattr(
        telegram_transcribe,
        "transcribe_private_audio",
        lambda path, **kwargs: calls.update(path=path, **kwargs) or {"ok": True, "text": "готово"},
    )

    assert telegram_transcribe.main(["--account", "work", "--file", str(audio)]) == 0
    assert calls["path"] == audio
    assert calls["inbox_dir"] == work_inbox
    assert calls["model_dir"] == work_model
    assert calls["system_owned_model"] is True
    assert json.loads(capsys.readouterr().out)["text"] == "готово"


def test_cli_work_rejects_a_personal_inbox_path(monkeypatch, tmp_path, capsys) -> None:
    work_inbox, _work_audio = _private_audio(tmp_path / "work")
    _personal_inbox, personal_audio = _private_audio(tmp_path / "personal")
    monkeypatch.setattr(
        telegram_transcribe,
        "account_media_paths",
        lambda _account: (work_inbox, tmp_path / "work-model"),
    )

    assert telegram_transcribe.main(["--account", "work", "--file", str(personal_audio)]) == 1
    assert json.loads(capsys.readouterr().out) == {"ok": False, "error": "audio_path_invalid"}
