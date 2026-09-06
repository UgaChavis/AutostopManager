from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import stat
import subprocess
from typing import Any

from .telegram_bridge import ACCOUNT_PATHS, BridgeError, account_inbox_dir, account_model_dir


DEFAULT_INBOX_DIR = account_inbox_dir("personal")
DEFAULT_MODEL_DIR = account_model_dir("personal")
MAX_AUDIO_BYTES = 25 * 1024 * 1024
MAX_AUDIO_DURATION_SECONDS = 10 * 60
PCM_SAMPLE_RATE = 16_000
MAX_DECODED_AUDIO_BYTES = MAX_AUDIO_DURATION_SECONDS * PCM_SAMPLE_RATE * 2
SUPPORTED_AUDIO_SUFFIXES = {".m4a", ".mp3", ".ogg", ".opus"}


class TranscriptionError(RuntimeError):
    def __init__(self, code: str, *, diagnostic: dict[str, str | int] | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.diagnostic = dict(diagnostic) if diagnostic else None


def _safe_backend_diagnostic(stage: str, exc: Exception) -> dict[str, str | int]:
    """Return only stable implementation details that cannot contain private media data."""

    diagnostic: dict[str, str | int] = {
        "stage": stage,
        "exception_module": type(exc).__module__,
        "exception_class": type(exc).__qualname__,
    }
    if isinstance(exc, OSError) and isinstance(exc.errno, int):
        diagnostic["errno"] = exc.errno
    return diagnostic


def _backend_failure(code: str, stage: str, exc: Exception) -> TranscriptionError:
    return TranscriptionError(code, diagnostic=_safe_backend_diagnostic(stage, exc))


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
                "-protocol_whitelist",
                "file,pipe",
                "-show_entries",
                "format=duration:stream=codec_type,codec_name",
                "-of",
                "json",
                "--",
                str(path),
            ],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
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


def _decode_private_audio(path: Path) -> Any:
    """Decode locally constrained input to PCM so Whisper never opens a media URL."""

    try:
        completed = subprocess.run(
            [
                "/usr/bin/ffmpeg",
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-protocol_whitelist",
                "file,pipe",
                "-i",
                str(path),
                "-map",
                "0:a:0",
                "-vn",
                "-sn",
                "-dn",
                "-t",
                str(MAX_AUDIO_DURATION_SECONDS),
                "-ac",
                "1",
                "-ar",
                str(PCM_SAMPLE_RATE),
                "-f",
                "s16le",
                "pipe:1",
            ],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=40,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise TranscriptionError("audio_decode_failed") from exc
    if (
        completed.returncode != 0
        or not completed.stdout
        or len(completed.stdout) > MAX_DECODED_AUDIO_BYTES
        or len(completed.stdout) % 2 != 0
    ):
        raise TranscriptionError("audio_decode_failed")
    try:
        import numpy as np

        return np.frombuffer(completed.stdout, dtype="<i2").astype("float32") / 32768.0
    except (ImportError, ValueError) as exc:
        raise TranscriptionError("audio_decode_failed") from exc


def _validate_local_model(model_dir: Path, *, system_owned_model: bool = False) -> None:
    if not model_dir.is_absolute() or not model_dir.is_dir():
        raise TranscriptionError("transcription_model_unavailable")
    try:
        model_stat = model_dir.stat()
    except OSError as exc:
        raise TranscriptionError("transcription_model_unavailable") from exc
    if system_owned_model:
        if model_stat.st_uid != 0 or model_stat.st_gid != os.getegid() or stat.S_IMODE(model_stat.st_mode) != 0o750:
            raise TranscriptionError("transcription_model_permissions_invalid")
        required_owner = 0
        required_group = os.getegid()
        required_mode = 0o640
    else:
        if model_stat.st_uid != os.geteuid() or stat.S_IMODE(model_stat.st_mode) != 0o700:
            raise TranscriptionError("transcription_model_permissions_invalid")
        required_owner = os.geteuid()
        required_group = model_stat.st_gid
        required_mode = 0o600
    required_files = (
        "config.json",
        "model.bin",
        "tokenizer.json",
        "vocabulary.txt",
    )
    for name in required_files:
        candidate = model_dir / name
        try:
            candidate_stat = candidate.lstat()
        except OSError as exc:
            raise TranscriptionError("transcription_model_invalid") from exc
        if (
            not stat.S_ISREG(candidate_stat.st_mode)
            or candidate_stat.st_uid != required_owner
            or candidate_stat.st_gid != required_group
            or stat.S_IMODE(candidate_stat.st_mode) != required_mode
        ):
            raise TranscriptionError("transcription_model_invalid")


def _load_model(model_dir: Path, *, cpu_threads: int, system_owned_model: bool = False) -> Any:
    _validate_local_model(model_dir, system_owned_model=system_owned_model)
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


def _start_model_transcription(
    model: Any,
    pcm_audio: Any,
    *,
    language: str,
    beam_size: int,
    vad_filter: bool,
) -> tuple[Any, Any]:
    return model.transcribe(
        pcm_audio,
        language=language,
        beam_size=beam_size,
        vad_filter=vad_filter,
        condition_on_previous_text=False,
    )


def _run_vad_self_check(pcm_audio: Any) -> None:
    """Load and execute the bundled VAD once without retaining any media."""

    from faster_whisper.vad import get_speech_timestamps

    get_speech_timestamps(pcm_audio)


def transcribe_private_audio(
    path: Path,
    *,
    language: str = "ru",
    inbox_dir: Path = DEFAULT_INBOX_DIR,
    model_dir: Path = DEFAULT_MODEL_DIR,
    system_owned_model: bool = False,
) -> dict[str, Any]:
    _validate_private_audio(path, inbox_dir=inbox_dir)
    probe = _probe_audio(path)
    pcm_audio = _decode_private_audio(path)
    cpu_threads = max(1, min(os.cpu_count() or 1, 4))
    model = _load_model(model_dir, cpu_threads=cpu_threads, system_owned_model=system_owned_model)
    vad_fallback_used = False
    vad_diagnostic: dict[str, str | int] | None = None
    try:
        segments, info = _start_model_transcription(
            model,
            pcm_audio,
            language=language,
            beam_size=5,
            vad_filter=True,
        )
    except Exception as vad_exc:  # noqa: BLE001 - VAD backend exception classes are provider-specific.
        vad_diagnostic = _safe_backend_diagnostic("vad_prepare", vad_exc)
        try:
            segments, info = _start_model_transcription(
                model,
                pcm_audio,
                language=language,
                beam_size=5,
                vad_filter=False,
            )
        except Exception as exc:
            diagnostic = _safe_backend_diagnostic("vad_fallback_prepare", exc)
            diagnostic["fallback_from"] = "vad_prepare"
            diagnostic["vad_exception_module"] = str(vad_diagnostic["exception_module"])
            diagnostic["vad_exception_class"] = str(vad_diagnostic["exception_class"])
            raise TranscriptionError("transcription_failed", diagnostic=diagnostic) from exc
        vad_fallback_used = True
    try:
        text = " ".join(str(segment.text).strip() for segment in segments if str(segment.text).strip()).strip()
    except Exception as exc:
        if vad_fallback_used:
            diagnostic = _safe_backend_diagnostic("vad_fallback_segment_inference", exc)
            diagnostic["fallback_from"] = "vad_prepare"
            if vad_diagnostic is not None:
                diagnostic["vad_exception_module"] = str(vad_diagnostic["exception_module"])
                diagnostic["vad_exception_class"] = str(vad_diagnostic["exception_class"])
            raise TranscriptionError("transcription_failed", diagnostic=diagnostic) from exc
        raise _backend_failure("transcription_failed", "segment_inference", exc) from exc
    if not text:
        raise TranscriptionError("transcription_empty")
    if len(text) > 32 * 1024:
        raise TranscriptionError("transcription_too_large")
    result = {
        "ok": True,
        "text": text,
        "language": str(getattr(info, "language", None) or language),
        "language_probability": round(float(getattr(info, "language_probability", 0.0) or 0.0), 4),
        **probe,
    }
    if vad_fallback_used:
        result["vad_fallback_used"] = True
    return result


def local_transcription_self_check(
    *,
    language: str = "ru",
    model_dir: Path = DEFAULT_MODEL_DIR,
    system_owned_model: bool = False,
) -> dict[str, Any]:
    """Exercise local VAD and inference in memory without opening private media."""

    try:
        import numpy as np
    except ImportError as exc:
        raise _backend_failure("transcription_self_check_failed", "runtime", exc) from exc

    cpu_threads = max(1, min(os.cpu_count() or 1, 4))
    model = _load_model(model_dir, cpu_threads=cpu_threads, system_owned_model=system_owned_model)
    silence = np.zeros(PCM_SAMPLE_RATE, dtype=np.float32)
    vad_diagnostic: dict[str, str | int] | None = None
    try:
        _run_vad_self_check(silence)
    except Exception as exc:  # noqa: BLE001 - VAD backend exception classes are provider-specific.
        vad_diagnostic = _safe_backend_diagnostic("vad_prepare", exc)
    try:
        segments, _info = _start_model_transcription(
            model,
            silence,
            language=language,
            beam_size=5,
            vad_filter=False,
        )
        for _segment in segments:
            pass
    except Exception as exc:
        diagnostic = _safe_backend_diagnostic("segment_inference", exc)
        if vad_diagnostic is not None:
            diagnostic["fallback_from"] = "vad_prepare"
        raise TranscriptionError("transcription_self_check_failed", diagnostic=diagnostic) from exc
    result: dict[str, Any] = {
        "ok": True,
        "check": {
            "vad_prepare": "degraded" if vad_diagnostic is not None else "ok",
            "segment_inference": "ok",
        },
    }
    if vad_diagnostic is not None:
        vad_diagnostic["fallback"] = "vad_filter_disabled"
        result["diagnostic"] = vad_diagnostic
    return result


def discard_private_audio(path: Path, *, inbox_dir: Path = DEFAULT_INBOX_DIR) -> None:
    _validate_private_audio(path, inbox_dir=inbox_dir)
    try:
        path.unlink()
    except OSError as exc:
        raise TranscriptionError("audio_cleanup_failed") from exc
    if path.exists() or path.is_symlink():
        raise TranscriptionError("audio_cleanup_failed")


def account_media_paths(account: str) -> tuple[Path, Path]:
    """Resolve fixed account-owned paths; callers cannot supply path overrides."""

    try:
        return account_inbox_dir(account), account_model_dir(account)
    except BridgeError as exc:
        raise TranscriptionError(exc.code) from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="autostop-telegram-transcribe")
    parser.add_argument("--account", choices=tuple(ACCOUNT_PATHS), required=True)
    input_source = parser.add_mutually_exclusive_group(required=True)
    input_source.add_argument("--file", type=Path)
    input_source.add_argument("--self-check", action="store_true")
    parser.add_argument("--language", default="ru")
    parser.add_argument("--delete-after", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.self_check and args.delete_after:
        raise SystemExit("--delete-after requires --file")
    payload: dict[str, Any]
    inbox_dir, model_dir = account_media_paths(args.account)
    try:
        if args.self_check:
            payload = local_transcription_self_check(
                language=args.language,
                model_dir=model_dir,
                system_owned_model=args.account == "work",
            )
        else:
            assert args.file is not None
            payload = transcribe_private_audio(
                args.file,
                language=args.language,
                inbox_dir=inbox_dir,
                model_dir=model_dir,
                system_owned_model=args.account == "work",
            )
    except TranscriptionError as exc:
        payload = {"ok": False, "error": exc.code}
        if exc.diagnostic:
            payload["diagnostic"] = exc.diagnostic
    finally:
        if args.delete_after and args.file is not None:
            try:
                discard_private_audio(args.file, inbox_dir=inbox_dir)
            except TranscriptionError:
                if payload.get("ok") is True:
                    payload = {"ok": False, "error": "audio_cleanup_failed"}
                else:
                    payload["cleanup_failed"] = True
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload.get("ok") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
