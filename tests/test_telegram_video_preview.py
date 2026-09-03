from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from autostop_manager import telegram_video_preview
from autostop_manager.telegram_video_preview import VideoPreviewError, build_private_video_storyboard


def _private_video(tmp_path: Path, content: bytes = b"\x00\x00\x00\x18ftypisomvideo") -> tuple[Path, Path]:
    inbox = tmp_path / "inbox"
    inbox.mkdir(mode=0o700, parents=True)
    video = inbox / "42-example.mp4"
    video.write_bytes(content)
    video.chmod(0o600)
    return inbox, video


def _completed(payload: dict) -> SimpleNamespace:
    return SimpleNamespace(returncode=0, stdout=json.dumps(payload))


def test_build_storyboard_returns_private_compact_preview(monkeypatch, tmp_path) -> None:
    inbox, video = _private_video(tmp_path)
    calls: dict[str, object] = {}

    monkeypatch.setattr(
        telegram_video_preview,
        "_probe_video",
        lambda _path: {
            "codec": "h264",
            "duration_seconds": 12.5,
            "width": 1920,
            "height": 1080,
            "audio_present": True,
        },
    )

    def render(video_path, output_path, *, duration, frame_count):
        calls.update(video=video_path, output=output_path, duration=duration, frame_count=frame_count)
        output_path.write_bytes(b"\xff\xd8\xffstoryboard\xff\xd9")
        output_path.chmod(0o600)

    monkeypatch.setattr(telegram_video_preview, "_render_storyboard", render)

    result = build_private_video_storyboard(video, inbox_dir=inbox)

    preview = Path(result["preview_path"])
    assert result == {
        "ok": True,
        "preview_path": str(preview),
        "frame_count": 8,
        "codec": "h264",
        "duration_seconds": 12.5,
        "width": 1920,
        "height": 1080,
        "audio_present": True,
    }
    assert preview.name == "42-example.preview.jpg"
    assert preview.stat().st_mode & 0o777 == 0o600
    assert calls["video"] == video
    assert calls["frame_count"] == 8


def test_private_video_rejects_symlink_and_open_permissions(tmp_path) -> None:
    inbox, video = _private_video(tmp_path)
    link = inbox / "link.mp4"
    link.symlink_to(video.name)

    with pytest.raises(VideoPreviewError, match="video_unavailable"):
        build_private_video_storyboard(link, inbox_dir=inbox)

    video.chmod(0o640)
    with pytest.raises(VideoPreviewError, match="video_permissions_or_size_invalid"):
        build_private_video_storyboard(video, inbox_dir=inbox)


def test_probe_accepts_one_bounded_video_and_optional_audio(monkeypatch, tmp_path) -> None:
    _inbox, video = _private_video(tmp_path)
    seen: dict[str, object] = {}

    def run(argv, **kwargs):
        seen["argv"] = argv
        seen["kwargs"] = kwargs
        return _completed(
            {
                "streams": [
                    {"codec_type": "video", "codec_name": "h264", "width": 1280, "height": 720},
                    {"codec_type": "audio", "codec_name": "aac"},
                ],
                "format": {"duration": "15.25"},
            }
        )

    monkeypatch.setattr(telegram_video_preview.subprocess, "run", run)

    assert telegram_video_preview._probe_video(video) == {
        "codec": "h264",
        "duration_seconds": 15.25,
        "width": 1280,
        "height": 720,
        "audio_present": True,
    }
    assert "file,pipe" in seen["argv"]
    assert seen["kwargs"]["stdout"] is telegram_video_preview.subprocess.PIPE
    assert seen["kwargs"]["stderr"] is telegram_video_preview.subprocess.DEVNULL
    assert seen["kwargs"]["timeout"] == 20


@pytest.mark.parametrize(
    "payload, error",
    [
        (
            {
                "streams": [
                    {"codec_type": "video", "codec_name": "h264", "width": 1280, "height": 720},
                    {"codec_type": "video", "codec_name": "h264", "width": 1280, "height": 720},
                ],
                "format": {"duration": "10"},
            },
            "video_streams_invalid",
        ),
        (
            {
                "streams": [{"codec_type": "video", "codec_name": "vp9", "width": 1280, "height": 720}],
                "format": {"duration": "10"},
            },
            "video_streams_invalid",
        ),
        (
            {
                "streams": [{"codec_type": "video", "codec_name": "h264", "width": 1280, "height": 720}],
                "format": {"duration": "121"},
            },
            "video_duration_invalid",
        ),
        (
            {
                "streams": [{"codec_type": "video", "codec_name": "h264", "width": 8192, "height": 4320}],
                "format": {"duration": "10"},
            },
            "video_dimensions_invalid",
        ),
    ],
)
def test_probe_rejects_unsafe_streams_duration_or_dimensions(monkeypatch, tmp_path, payload, error) -> None:
    _inbox, video = _private_video(tmp_path)
    monkeypatch.setattr(
        telegram_video_preview.subprocess,
        "run",
        lambda *_args, **_kwargs: _completed(payload),
    )

    with pytest.raises(VideoPreviewError, match=error):
        telegram_video_preview._probe_video(video)


def test_render_storyboard_is_local_bounded_and_validates_jpeg(monkeypatch, tmp_path) -> None:
    _inbox, video = _private_video(tmp_path)
    preview = video.with_name("preview.jpg")
    preview.write_bytes(b"")
    preview.chmod(0o600)
    seen: dict[str, object] = {}

    def run(argv, **kwargs):
        seen["argv"] = argv
        seen["kwargs"] = kwargs
        preview.write_bytes(b"\xff\xd8\xffframes\xff\xd9")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(telegram_video_preview.subprocess, "run", run)

    telegram_video_preview._render_storyboard(video, preview, duration=20.0, frame_count=8)

    argv = seen["argv"]
    assert argv[0] == "/usr/bin/ffmpeg"
    assert "file,pipe" in argv
    assert "-an" in argv
    assert "-frames:v" in argv
    assert seen["kwargs"]["timeout"] == 30


def test_cli_delete_after_removes_exact_video_and_keeps_preview(monkeypatch, tmp_path, capsys) -> None:
    inbox, video = _private_video(tmp_path)
    preview = inbox / "42-example.preview.jpg"
    preview.write_bytes(b"\xff\xd8\xffframes\xff\xd9")
    preview.chmod(0o600)
    monkeypatch.setattr(telegram_video_preview, "account_inbox_path", lambda _account: inbox)
    monkeypatch.setattr(
        telegram_video_preview,
        "build_private_video_storyboard",
        lambda *_args, **_kwargs: {"ok": True, "preview_path": str(preview), "frame_count": 8},
    )

    assert telegram_video_preview.main(["--account", "work", "--file", str(video), "--delete-after"]) == 0
    assert not video.exists()
    assert preview.exists()
    assert json.loads(capsys.readouterr().out)["preview_path"] == str(preview)


def test_cli_cleanup_failure_preserves_exact_preview_path(monkeypatch, tmp_path, capsys) -> None:
    inbox, video = _private_video(tmp_path)
    preview = inbox / "42-example.preview.jpg"
    preview.write_bytes(b"\xff\xd8\xffframes\xff\xd9")
    preview.chmod(0o600)
    monkeypatch.setattr(telegram_video_preview, "account_inbox_path", lambda _account: inbox)
    monkeypatch.setattr(
        telegram_video_preview,
        "build_private_video_storyboard",
        lambda *_args, **_kwargs: {"ok": True, "preview_path": str(preview), "frame_count": 8},
    )
    monkeypatch.setattr(
        telegram_video_preview,
        "discard_private_video",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(VideoPreviewError("video_cleanup_failed")),
    )

    assert telegram_video_preview.main(["--account", "work", "--file", str(video), "--delete-after"]) == 1
    assert json.loads(capsys.readouterr().out) == {
        "ok": False,
        "error": "video_cleanup_failed",
        "preview_path": str(preview),
    }


def test_cli_delete_after_preserves_processing_error_after_removing_valid_video(monkeypatch, tmp_path, capsys) -> None:
    inbox, video = _private_video(tmp_path)
    monkeypatch.setattr(telegram_video_preview, "account_inbox_path", lambda _account: inbox)
    monkeypatch.setattr(
        telegram_video_preview,
        "build_private_video_storyboard",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(VideoPreviewError("storyboard_render_failed")),
    )

    assert telegram_video_preview.main(["--account", "work", "--file", str(video), "--delete-after"]) == 1
    assert not video.exists()
    assert json.loads(capsys.readouterr().out) == {"ok": False, "error": "storyboard_render_failed"}


def test_cli_delete_after_keeps_video_processing_error_when_cleanup_also_fails(monkeypatch, tmp_path, capsys) -> None:
    inbox, video = _private_video(tmp_path)
    monkeypatch.setattr(telegram_video_preview, "account_inbox_path", lambda _account: inbox)
    monkeypatch.setattr(
        telegram_video_preview,
        "build_private_video_storyboard",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(VideoPreviewError("storyboard_render_failed")),
    )
    monkeypatch.setattr(
        telegram_video_preview,
        "discard_private_video",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(VideoPreviewError("video_cleanup_failed")),
    )

    assert telegram_video_preview.main(["--account", "work", "--file", str(video), "--delete-after"]) == 1
    assert json.loads(capsys.readouterr().out) == {
        "ok": False,
        "error": "storyboard_render_failed",
        "cleanup_failed": True,
    }


def test_cli_requires_an_explicit_account() -> None:
    parser = telegram_video_preview.build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(["--file", "/run/example.mp4"])


def test_cli_work_uses_only_its_fixed_inbox(monkeypatch, tmp_path, capsys) -> None:
    work_inbox, video = _private_video(tmp_path / "work")
    calls: dict[str, object] = {}
    monkeypatch.setattr(
        telegram_video_preview,
        "account_inbox_path",
        lambda account: work_inbox if account == "work" else pytest.fail("wrong account"),
    )
    monkeypatch.setattr(
        telegram_video_preview,
        "build_private_video_storyboard",
        lambda path, **kwargs: (
            calls.update(path=path, **kwargs) or {"ok": True, "preview_path": str(work_inbox / "preview.jpg")}
        ),
    )

    assert telegram_video_preview.main(["--account", "work", "--file", str(video)]) == 0
    assert calls == {"path": video, "inbox_dir": work_inbox}
    assert json.loads(capsys.readouterr().out)["ok"] is True


def test_cli_work_rejects_a_personal_inbox_path(monkeypatch, tmp_path, capsys) -> None:
    work_inbox, _work_video = _private_video(tmp_path / "work")
    _personal_inbox, personal_video = _private_video(tmp_path / "personal")
    monkeypatch.setattr(telegram_video_preview, "account_inbox_path", lambda _account: work_inbox)

    assert telegram_video_preview.main(["--account", "work", "--file", str(personal_video)]) == 1
    assert json.loads(capsys.readouterr().out) == {
        "ok": False,
        "error": "video_permissions_or_size_invalid",
    }
