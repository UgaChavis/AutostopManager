from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from autostop_manager import telegram_transcribe
from autostop_manager.telegram_transcribe import TranscriptionError, transcribe_private_audio


def _private_audio(tmp_path: Path, content: bytes = b"OggSvoice") -> tuple[Path, Path]:
    inbox = tmp_path / "inbox"
    inbox.mkdir(mode=0o700)
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
    monkeypatch.setattr(telegram_transcribe, "_load_model", lambda _path, cpu_threads: Model())

    result = transcribe_private_audio(audio, inbox_dir=inbox, model_dir=model_dir)

    assert result["text"] == "Привет из сервиса"
    assert result["language"] == "ru"
    assert result["duration_seconds"] == 4.2
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


def test_cli_delete_after_removes_exact_private_audio(monkeypatch, tmp_path, capsys) -> None:
    inbox, audio = _private_audio(tmp_path)
    monkeypatch.setattr(telegram_transcribe, "DEFAULT_INBOX_DIR", inbox)
    monkeypatch.setattr(
        telegram_transcribe,
        "transcribe_private_audio",
        lambda *_args, **_kwargs: {"ok": True, "text": "готово"},
    )

    assert telegram_transcribe.main(["--file", str(audio), "--delete-after"]) == 0
    assert not audio.exists()
    assert json.loads(capsys.readouterr().out)["text"] == "готово"


def test_cli_delete_after_reports_cleanup_failure(monkeypatch, tmp_path, capsys) -> None:
    inbox, audio = _private_audio(tmp_path)
    monkeypatch.setattr(telegram_transcribe, "DEFAULT_INBOX_DIR", inbox)
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

    assert telegram_transcribe.main(["--file", str(audio), "--delete-after"]) == 1
    assert json.loads(capsys.readouterr().out) == {"ok": False, "error": "audio_cleanup_failed"}


def test_model_loader_fails_closed_without_local_model(tmp_path) -> None:
    with pytest.raises(TranscriptionError, match="transcription_model_unavailable"):
        telegram_transcribe._load_model(tmp_path / "missing", cpu_threads=2)


def test_model_validation_requires_pinned_revision_marker(tmp_path) -> None:
    model_dir = tmp_path / "model"
    model_dir.mkdir(mode=0o700)
    for name in ("config.json", "model.bin", "tokenizer.json", "vocabulary.txt"):
        candidate = model_dir / name
        candidate.write_bytes(b"model")
        candidate.chmod(0o600)

    with pytest.raises(TranscriptionError, match="transcription_model_invalid"):
        telegram_transcribe._validate_local_model(model_dir)
