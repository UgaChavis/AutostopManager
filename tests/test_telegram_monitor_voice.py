from __future__ import annotations

import pytest

from autostop_manager import telegram_monitor_voice


def test_monitored_voice_self_check_exercises_media_runner(monkeypatch, tmp_path) -> None:
    runner = tmp_path / "run-work-telegram-media.sh"
    monkeypatch.setattr(telegram_monitor_voice.os, "geteuid", lambda: 0)
    monkeypatch.setattr(telegram_monitor_voice, "_media_runner_path", lambda: runner)
    assert telegram_monitor_voice.self_check() == {"ok": True, "check": "media_runner_ready"}


def test_monitored_voice_stage_uses_a_timeout_longer_than_the_server_deadline(monkeypatch) -> None:
    timeouts: list[float] = []
    monkeypatch.setattr(
        telegram_monitor_voice,
        "send_local_request",
        lambda *_args, timeout_seconds: timeouts.append(timeout_seconds) or {"ok": True},
    )
    assert telegram_monitor_voice._request({"operation": "monitor_voice_stage"}) == {"ok": True}
    assert telegram_monitor_voice._request({"operation": "monitor_voice_discard"}) == {"ok": True}
    assert timeouts == [
        telegram_monitor_voice._MONITOR_VOICE_STAGE_RPC_TIMEOUT_SECONDS,
        telegram_monitor_voice.DEFAULT_LOCAL_REQUEST_TIMEOUT_SECONDS,
    ]
    assert timeouts[0] > telegram_monitor_voice.MONITOR_VOICE_STAGE_TIMEOUT_SECONDS


def test_monitored_voice_hides_handle_and_path(monkeypatch, tmp_path) -> None:
    handle = "b" * 32
    requests: list[dict[str, object]] = []
    seen: dict[str, object] = {}

    monkeypatch.setattr(telegram_monitor_voice.os, "geteuid", lambda: 0)

    def request(payload):
        requests.append(payload)
        operation = payload["operation"]
        if operation == "monitor_voice_stage":
            return {
                "ok": True,
                "event_id": "inbound-1",
                "media": {"kind": "voice", "duration_seconds": 8, "suffix": ".ogg"},
                "media_handle": handle,
            }
        assert operation == "monitor_voice_discard"
        return {"ok": True, "cleanup_verified": True}

    expected_path = tmp_path / f"monitor-{handle}.ogg"
    monkeypatch.setattr(telegram_monitor_voice, "_request", request)
    monkeypatch.setattr(telegram_monitor_voice, "_staged_voice_path", lambda *_args: expected_path)
    monkeypatch.setattr(
        telegram_monitor_voice,
        "_run_transcriber",
        lambda path, *, language: (
            seen.update(path=path, language=language)
            or {"transcript": "Готово", "language": "ru", "cleanup_verified": True}
        ),
    )

    result = telegram_monitor_voice.transcribe_monitored_voice("inbound-1")

    assert result == {
        "ok": True,
        "event_id": "inbound-1",
        "transcript": "Готово",
        "language": "ru",
        "duration_seconds": 8,
        "cleanup_verified": True,
    }
    assert seen == {"path": expected_path, "language": "ru"}
    assert requests == [
        {"operation": "monitor_voice_stage", "event_id": "inbound-1"},
        {"operation": "monitor_voice_discard", "media_handle": handle, "runner_cleanup_verified": True},
    ]
    serialized = str(result)
    assert handle not in serialized
    assert str(expected_path) not in serialized


def test_monitored_voice_apply_fails_closed_when_cleanup_is_not_verified(monkeypatch, tmp_path) -> None:
    handle = "b" * 32

    monkeypatch.setattr(telegram_monitor_voice.os, "geteuid", lambda: 0)
    monkeypatch.setattr(telegram_monitor_voice, "_staged_voice_path", lambda *_args: tmp_path / "private.ogg")
    monkeypatch.setattr(
        telegram_monitor_voice, "_run_transcriber", lambda *_args, **_kwargs: {"transcript": "text", "language": "ru"}
    )

    def request(payload):
        if payload["operation"] == "monitor_voice_stage":
            return {
                "ok": True,
                "media": {"kind": "voice", "duration_seconds": 8, "suffix": ".ogg"},
                "media_handle": handle,
            }
        raise telegram_monitor_voice.MonitorVoiceError("inbound_voice_stage_unavailable")

    monkeypatch.setattr(telegram_monitor_voice, "_request", request)

    with pytest.raises(telegram_monitor_voice.MonitorVoiceError, match="monitor_voice_cleanup_failed"):
        telegram_monitor_voice.transcribe_monitored_voice("inbound-1")


def test_monitored_voice_requires_root(monkeypatch) -> None:
    monkeypatch.setattr(telegram_monitor_voice.os, "geteuid", lambda: 1000)

    with pytest.raises(telegram_monitor_voice.MonitorVoiceError, match="monitor_voice_root_required"):
        telegram_monitor_voice.transcribe_monitored_voice("inbound-1")
