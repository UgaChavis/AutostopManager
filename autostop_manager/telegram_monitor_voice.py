from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

from .telegram_bridge import BridgeError, WORK_SOCKET_PATH, account_inbox_dir, send_local_request


_HANDLE_PATTERN = re.compile(r"[A-Za-z0-9_-]{24,128}")
_SUFFIXES = frozenset({".m4a", ".mp3", ".ogg", ".opus"})
_LANGUAGE_PATTERN = re.compile(r"[a-z]{2,8}")
_ERROR_PATTERN = re.compile(r"[a-z0-9_]{3,96}")
_MAX_RUNNER_OUTPUT_BYTES = 64 * 1024


class MonitorVoiceError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _safe_error(value: Any, *, fallback: str = "monitor_voice_failed") -> str:
    candidate = str(value or "").strip().casefold()
    return candidate if _ERROR_PATTERN.fullmatch(candidate) else fallback


def _require_root() -> None:
    if os.geteuid() != 0:
        raise MonitorVoiceError("monitor_voice_root_required")


def _request(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        response = send_local_request(WORK_SOCKET_PATH, payload)
    except BridgeError as exc:
        raise MonitorVoiceError(exc.code) from exc
    if response.get("ok") is not True:
        raise MonitorVoiceError(_safe_error(response.get("error")))
    return response


def _media_runner_path() -> Path:
    candidate = Path(__file__).resolve().parents[1] / "scripts" / "run-work-telegram-media.sh"
    if not candidate.is_file() or candidate.is_symlink() or not os.access(candidate, os.X_OK):
        raise MonitorVoiceError("monitor_voice_runner_unavailable")
    return candidate


def _staged_voice_path(handle: str, suffix: str) -> Path:
    if _HANDLE_PATTERN.fullmatch(handle) is None or suffix not in _SUFFIXES:
        raise MonitorVoiceError("monitor_voice_stage_invalid")
    return account_inbox_dir("work") / f"monitor-{handle}{suffix}"


def _run_transcriber(path: Path, *, language: str) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            [
                str(_media_runner_path()),
                "transcribe",
                "--file",
                str(path),
                "--language",
                language,
                "--delete-after",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=180,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MonitorVoiceError("monitor_voice_transcription_unavailable") from exc
    if len(completed.stdout.encode("utf-8", errors="ignore")) > _MAX_RUNNER_OUTPUT_BYTES:
        raise MonitorVoiceError("monitor_voice_transcription_invalid")
    try:
        payload = json.loads(completed.stdout)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise MonitorVoiceError("monitor_voice_transcription_invalid") from exc
    if not isinstance(payload, dict):
        raise MonitorVoiceError("monitor_voice_transcription_invalid")
    if completed.returncode != 0 or payload.get("ok") is not True:
        raise MonitorVoiceError(_safe_error(payload.get("error"), fallback="monitor_voice_transcription_failed"))
    transcript = payload.get("text")
    language_value = payload.get("language")
    if not isinstance(transcript, str) or not transcript or len(transcript) > 32 * 1024:
        raise MonitorVoiceError("monitor_voice_transcription_invalid")
    if not isinstance(language_value, str) or _LANGUAGE_PATTERN.fullmatch(language_value) is None:
        raise MonitorVoiceError("monitor_voice_transcription_invalid")
    return {"transcript": transcript, "language": language_value}


def transcribe_monitored_voice(event_id: str, *, language: str = "ru") -> dict[str, Any]:
    _require_root()
    if re.fullmatch(r"inbound-[1-9][0-9]{0,11}", event_id) is None:
        raise MonitorVoiceError("inbound_event_invalid")
    if _LANGUAGE_PATTERN.fullmatch(language) is None:
        raise MonitorVoiceError("monitor_voice_language_invalid")
    handle = ""
    cleanup_verified = False
    try:
        staged = _request(
            {
                "operation": "monitor_voice_stage",
                "event_id": event_id,
            }
        )
        handle = str(staged.get("media_handle") or "")
        staged_media = staged.get("media")
        if not isinstance(staged_media, dict) or staged_media.get("kind") != "voice":
            raise MonitorVoiceError("monitor_voice_stage_invalid")
        duration = staged_media.get("duration_seconds")
        if not isinstance(duration, int) or duration <= 0:
            raise MonitorVoiceError("monitor_voice_stage_invalid")
        path = _staged_voice_path(handle, str(staged_media.get("suffix") or ""))
        transcription = _run_transcriber(path, language=language)
    finally:
        if _HANDLE_PATTERN.fullmatch(handle) is not None:
            try:
                cleanup = _request({"operation": "monitor_voice_discard", "media_handle": handle})
                cleanup_verified = cleanup.get("cleanup_verified") is True
            except MonitorVoiceError:
                cleanup_verified = False
    if not cleanup_verified:
        raise MonitorVoiceError("monitor_voice_cleanup_failed")
    return {
        "ok": True,
        "event_id": event_id,
        "transcript": transcription["transcript"],
        "language": transcription["language"],
        "duration_seconds": duration,
        "cleanup_verified": True,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="autostop-work-telegram-monitor-voice")
    parser.add_argument("--event-id", required=True)
    parser.add_argument("--language", default="ru")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        payload = transcribe_monitored_voice(args.event_id, language=args.language)
    except MonitorVoiceError as exc:
        payload = {"ok": False, "error": exc.code}
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload.get("ok") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
