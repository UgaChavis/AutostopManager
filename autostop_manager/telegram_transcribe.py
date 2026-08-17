from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import stat
import subprocess
from typing import Any


DEFAULT_INBOX_DIR = Path("/run/autostop-telegram/inbox")
DEFAULT_MODEL_DIR = Path("/var/lib/autostop-telegram/models/faster-whisper-small")
MODEL_REVISION = "536b0662742c02347bc0e980a01041f333bce120"
MAX_AUDIO_BYTES = 25 * 1024 * 1024
MAX_AUDIO_DURATION_SECONDS = 10 * 60
SUPPORTED_AUDIO_SUFFIXES = {".m4a", ".mp3", ".ogg", ".opus"}


class TranscriptionError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _validate_private_audio(path: Path, *, inbox_dir: Path = DEFAULT_INBOX_DIR) -> None:
    if (
        not path.is_absolute()
        or path.parent != inbox_dir
        or path.name in {"", ".", ".."}
        or path.suffix.casefold() not in SUPPORTED_AUDIO_SUFFIXES
    ):
        raise TranscriptionError("audio_path_invalid")
    try:
        inbox_fd = os.open(inbox_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError as exc:
        raise TranscriptionError("audio_inbox_unavailable") from exc
    try:
        inbox_stat = os.fstat(inbox_fd)
        if inbox_stat.st_uid != os.geteuid() or stat.S_IMODE(inbox_stat.st_mode) != 0o700:
            raise TranscriptionError("audio_inbox_permissions_invalid")
        try:
            audio_fd = os.open(path.name, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=inbox_fd)
        except OSError as exc:
            raise TranscriptionError("audio_unavailable") from exc
        try:
            audio_stat = os.fstat(audio_fd)
            if (
                not stat.S_ISREG(audio_stat.st_mode)
                or audio_stat.st_uid != os.geteuid()
                or stat.S_IMODE(audio_stat.st_mode) != 0o600
                or not 0 < audio_stat.st_size <= MAX_AUDIO_BYTES
            ):
                raise TranscriptionError("audio_permissions_or_size_invalid")
        finally:
            os.close(audio_fd)
    finally:
        os.close(inbox_fd)


def _probe_audio(path: Path) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            [
                "/usr/bin/ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration:stream=codec_type,codec_name",
                "-of",
                "json",
                "--",
                str(path),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise TranscriptionError("audio_probe_failed") from exc
    if completed.returncode != 0 or len(completed.stdout) > 64 * 1024:
        raise TranscriptionError("audio_probe_failed")
    try:
        payload = json.loads(completed.stdout)
        streams = payload.get("streams") or []
        duration = float((payload.get("format") or {}).get("duration") or 0)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise TranscriptionError("audio_probe_invalid") from exc
    audio_streams = [stream for stream in streams if stream.get("codec_type") == "audio"]
    if len(audio_streams) != 1 or any(stream.get("codec_type") == "video" for stream in streams):
        raise TranscriptionError("audio_streams_invalid")
    if not 0 < duration <= MAX_AUDIO_DURATION_SECONDS:
        raise TranscriptionError("audio_duration_invalid")
    return {
        "codec": str(audio_streams[0].get("codec_name") or ""),
        "duration_seconds": round(duration, 3),
    }


def _validate_local_model(model_dir: Path) -> None:
    if not model_dir.is_absolute() or not model_dir.is_dir():
        raise TranscriptionError("transcription_model_unavailable")
    try:
        model_stat = model_dir.stat()
    except OSError as exc:
        raise TranscriptionError("transcription_model_unavailable") from exc
    if model_stat.st_uid != os.geteuid() or stat.S_IMODE(model_stat.st_mode) != 0o700:
        raise TranscriptionError("transcription_model_permissions_invalid")
    required_files = (
        "config.json",
        "model.bin",
        "tokenizer.json",
        "vocabulary.txt",
        f".cache/huggingface/trees/{MODEL_REVISION}.json",
    )
    for name in required_files:
        candidate = model_dir / name
        try:
            candidate_stat = candidate.lstat()
        except OSError as exc:
            raise TranscriptionError("transcription_model_invalid") from exc
        if (
            not stat.S_ISREG(candidate_stat.st_mode)
            or candidate_stat.st_uid != os.geteuid()
            or stat.S_IMODE(candidate_stat.st_mode) != 0o600
        ):
            raise TranscriptionError("transcription_model_invalid")


def _load_model(model_dir: Path, *, cpu_threads: int) -> Any:
    _validate_local_model(model_dir)
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise TranscriptionError("faster_whisper_unavailable") from exc
    try:
        return WhisperModel(
            str(model_dir),
            device="cpu",
            compute_type="int8",
            cpu_threads=cpu_threads,
            num_workers=1,
            local_files_only=True,
        )
    except Exception as exc:
        raise TranscriptionError("transcription_model_load_failed") from exc


def transcribe_private_audio(
    path: Path,
    *,
    language: str = "ru",
    inbox_dir: Path = DEFAULT_INBOX_DIR,
    model_dir: Path = DEFAULT_MODEL_DIR,
) -> dict[str, Any]:
    _validate_private_audio(path, inbox_dir=inbox_dir)
    probe = _probe_audio(path)
    cpu_threads = max(1, min(os.cpu_count() or 1, 4))
    model = _load_model(model_dir, cpu_threads=cpu_threads)
    try:
        segments, info = model.transcribe(
            str(path),
            language=language,
            beam_size=5,
            vad_filter=True,
            condition_on_previous_text=False,
        )
        text = " ".join(str(segment.text).strip() for segment in segments if str(segment.text).strip()).strip()
    except Exception as exc:
        raise TranscriptionError("transcription_failed") from exc
    if not text:
        raise TranscriptionError("transcription_empty")
    if len(text) > 32 * 1024:
        raise TranscriptionError("transcription_too_large")
    return {
        "ok": True,
        "text": text,
        "language": str(getattr(info, "language", None) or language),
        "language_probability": round(float(getattr(info, "language_probability", 0.0) or 0.0), 4),
        **probe,
    }


def discard_private_audio(path: Path, *, inbox_dir: Path = DEFAULT_INBOX_DIR) -> None:
    _validate_private_audio(path, inbox_dir=inbox_dir)
    try:
        path.unlink()
    except OSError as exc:
        raise TranscriptionError("audio_cleanup_failed") from exc
    if path.exists() or path.is_symlink():
        raise TranscriptionError("audio_cleanup_failed")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="autostop-telegram-transcribe")
    parser.add_argument("--file", required=True, type=Path)
    parser.add_argument("--language", default="ru")
    parser.add_argument("--delete-after", action="store_true")
    parser.add_argument("--model-dir", type=Path, default=DEFAULT_MODEL_DIR)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    payload: dict[str, Any]
    try:
        payload = transcribe_private_audio(args.file, language=args.language, model_dir=args.model_dir)
    except TranscriptionError as exc:
        payload = {"ok": False, "error": exc.code}
    finally:
        if args.delete_after:
            try:
                discard_private_audio(args.file, inbox_dir=DEFAULT_INBOX_DIR)
            except TranscriptionError:
                payload = {"ok": False, "error": "audio_cleanup_failed"}
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload.get("ok") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
