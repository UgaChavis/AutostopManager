from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import stat
import subprocess
from typing import Any

from .telegram_bridge import ACCOUNT_PATHS, BridgeError, _read_private_download, account_inbox_dir


DEFAULT_INBOX_DIR = account_inbox_dir("personal")
MAX_VIDEO_BYTES = 25 * 1024 * 1024
MAX_VIDEO_DURATION_SECONDS = 2 * 60
MAX_VIDEO_PIXELS = 4096 * 2160
MAX_STORYBOARD_BYTES = 12 * 1024 * 1024
DEFAULT_FRAME_COUNT = 8
SUPPORTED_VIDEO_CODECS = frozenset({"h264", "hevc", "mpeg4"})


class VideoPreviewError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _validate_private_file(
    path: Path,
    *,
    inbox_dir: Path,
    suffixes: frozenset[str],
    max_bytes: int,
    unavailable_code: str,
    invalid_code: str,
) -> os.stat_result:
    if (
        not path.is_absolute()
        or path.parent != inbox_dir
        or path.name in {"", ".", ".."}
        or path.suffix.casefold() not in suffixes
    ):
        raise VideoPreviewError(invalid_code)
    try:
        inbox_fd = os.open(inbox_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError as exc:
        raise VideoPreviewError("video_inbox_unavailable") from exc
    try:
        inbox_stat = os.fstat(inbox_fd)
        if inbox_stat.st_uid != os.geteuid() or stat.S_IMODE(inbox_stat.st_mode) != 0o700:
            raise VideoPreviewError("video_inbox_permissions_invalid")
        try:
            file_fd = os.open(path.name, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=inbox_fd)
        except OSError as exc:
            raise VideoPreviewError(unavailable_code) from exc
        try:
            file_stat = os.fstat(file_fd)
            if (
                not stat.S_ISREG(file_stat.st_mode)
                or file_stat.st_uid != os.geteuid()
                or stat.S_IMODE(file_stat.st_mode) != 0o600
                or not 0 < file_stat.st_size <= max_bytes
            ):
                raise VideoPreviewError(invalid_code)
            return file_stat
        finally:
            os.close(file_fd)
    finally:
        os.close(inbox_fd)


def _validate_private_video(path: Path, *, inbox_dir: Path = DEFAULT_INBOX_DIR) -> None:
    _validate_private_file(
        path,
        inbox_dir=inbox_dir,
        suffixes=frozenset({".mp4"}),
        max_bytes=MAX_VIDEO_BYTES,
        unavailable_code="video_unavailable",
        invalid_code="video_permissions_or_size_invalid",
    )


def _probe_video(path: Path) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            [
                "/usr/bin/ffprobe",
                "-v",
                "error",
                "-protocol_whitelist",
                "file,pipe",
                "-show_entries",
                "format=duration:stream=codec_type,codec_name,width,height",
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
        raise VideoPreviewError("video_probe_failed") from exc
    if completed.returncode != 0 or len(completed.stdout) > 64 * 1024:
        raise VideoPreviewError("video_probe_failed")
    try:
        payload = json.loads(completed.stdout)
        streams = payload.get("streams") or []
        duration = float((payload.get("format") or {}).get("duration") or 0)
        video_streams = [stream for stream in streams if stream.get("codec_type") == "video"]
        audio_streams = [stream for stream in streams if stream.get("codec_type") == "audio"]
        width = int(video_streams[0].get("width") or 0)
        height = int(video_streams[0].get("height") or 0)
        codec = str(video_streams[0].get("codec_name") or "").casefold()
    except (IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise VideoPreviewError("video_probe_invalid") from exc
    if (
        len(video_streams) != 1
        or len(audio_streams) > 1
        or len(video_streams) + len(audio_streams) != len(streams)
        or codec not in SUPPORTED_VIDEO_CODECS
    ):
        raise VideoPreviewError("video_streams_invalid")
    if not 0 < duration <= MAX_VIDEO_DURATION_SECONDS:
        raise VideoPreviewError("video_duration_invalid")
    if (
        width <= 0
        or height <= 0
        or width * height > MAX_VIDEO_PIXELS
        or max(width, height) > 4096
        or min(width, height) > 2160
    ):
        raise VideoPreviewError("video_dimensions_invalid")
    return {
        "codec": codec,
        "duration_seconds": round(duration, 3),
        "width": width,
        "height": height,
        "audio_present": bool(audio_streams),
    }


def _storyboard_path(video_path: Path) -> Path:
    return video_path.with_name(f"{video_path.stem}.preview.jpg")


def _reserve_storyboard(path: Path, *, inbox_dir: Path) -> None:
    if path.parent != inbox_dir or path.suffix.casefold() != ".jpg":
        raise VideoPreviewError("storyboard_path_invalid")
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
    except OSError as exc:
        raise VideoPreviewError("storyboard_unavailable") from exc
    os.close(descriptor)


def _render_storyboard(video_path: Path, output_path: Path, *, duration: float, frame_count: int) -> None:
    frame_rate = frame_count / duration
    filter_graph = (
        f"fps={frame_rate:.8f},"
        "scale=640:360:force_original_aspect_ratio=decrease,"
        "pad=640:360:(ow-iw)/2:(oh-ih)/2:black,"
        f"tile=4x2:nb_frames={frame_count}:padding=4:margin=4:color=black"
    )
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
                "-threads",
                "1",
                "-filter_threads",
                "1",
                "-i",
                str(video_path),
                "-map",
                "0:v:0",
                "-an",
                "-sn",
                "-dn",
                "-t",
                f"{duration:.3f}",
                "-vf",
                filter_graph,
                "-frames:v",
                "1",
                "-q:v",
                "3",
                "-y",
                str(output_path),
            ],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise VideoPreviewError("storyboard_render_failed") from exc
    if completed.returncode != 0:
        raise VideoPreviewError("storyboard_render_failed")
    try:
        os.chmod(output_path, 0o600)
        content = _read_private_download(output_path, inbox_dir=output_path.parent)
    except (BridgeError, OSError) as exc:
        raise VideoPreviewError("storyboard_render_failed") from exc
    if (
        not 0 < len(content) <= MAX_STORYBOARD_BYTES
        or not content.startswith(b"\xff\xd8\xff")
        or not content.endswith(b"\xff\xd9")
    ):
        raise VideoPreviewError("storyboard_invalid")


def build_private_video_storyboard(
    path: Path,
    *,
    inbox_dir: Path = DEFAULT_INBOX_DIR,
    frame_count: int = DEFAULT_FRAME_COUNT,
) -> dict[str, Any]:
    if frame_count != DEFAULT_FRAME_COUNT:
        raise VideoPreviewError("storyboard_frame_count_invalid")
    _validate_private_video(path, inbox_dir=inbox_dir)
    probe = _probe_video(path)
    output_path = _storyboard_path(path)
    _reserve_storyboard(output_path, inbox_dir=inbox_dir)
    try:
        _render_storyboard(
            path,
            output_path,
            duration=float(probe["duration_seconds"]),
            frame_count=frame_count,
        )
        _validate_private_file(
            output_path,
            inbox_dir=inbox_dir,
            suffixes=frozenset({".jpg"}),
            max_bytes=MAX_STORYBOARD_BYTES,
            unavailable_code="storyboard_unavailable",
            invalid_code="storyboard_invalid",
        )
    except BaseException:
        output_path.unlink(missing_ok=True)
        raise
    return {
        "ok": True,
        "preview_path": str(output_path),
        "frame_count": frame_count,
        **probe,
    }


def discard_private_video(path: Path, *, inbox_dir: Path = DEFAULT_INBOX_DIR) -> None:
    _validate_private_video(path, inbox_dir=inbox_dir)
    try:
        path.unlink()
    except OSError as exc:
        raise VideoPreviewError("video_cleanup_failed") from exc
    if path.exists() or path.is_symlink():
        raise VideoPreviewError("video_cleanup_failed")


def account_inbox_path(account: str) -> Path:
    """Resolve the fixed private inbox for one selected account."""

    try:
        return account_inbox_dir(account)
    except BridgeError as exc:
        raise VideoPreviewError(exc.code) from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="autostop-telegram-video-preview")
    parser.add_argument("--account", choices=tuple(ACCOUNT_PATHS), required=True)
    parser.add_argument("--file", required=True, type=Path)
    parser.add_argument("--delete-after", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    payload: dict[str, Any] = {"ok": False, "error": "video_preview_failed"}
    inbox_dir = account_inbox_path(args.account)
    try:
        payload = build_private_video_storyboard(args.file, inbox_dir=inbox_dir)
    except VideoPreviewError as exc:
        payload = {"ok": False, "error": exc.code}
    except Exception:  # noqa: BLE001 - CLI boundary must not expose decoder details.
        payload = {"ok": False, "error": "video_preview_failed"}
    finally:
        if args.delete_after:
            try:
                discard_private_video(args.file, inbox_dir=inbox_dir)
            except VideoPreviewError:
                if payload.get("ok") is True:
                    preview_path = payload.get("preview_path")
                    payload = {"ok": False, "error": "video_cleanup_failed"}
                    if isinstance(preview_path, str):
                        payload["preview_path"] = preview_path
                else:
                    payload["cleanup_failed"] = True
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload.get("ok") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
