"""Owner-requested capture from the private home camera through ``home-pc``."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import errno
from fractions import Fraction
import ipaddress
import json
import os
from pathlib import Path
import secrets
import socket
import stat
import subprocess
import time
from typing import Any, Literal
from urllib.parse import quote


DEFAULT_CONFIG_PATH = Path("/etc/autostop-camera/home-tapo-c225.json")
_EXPECTED_CONFIG_DIRECTORY_MODE = 0o700
_EXPECTED_CONFIG_MODE = 0o600
_EXPECTED_OUTPUT_DIRECTORY_MODE = 0o700
_STREAM_PATHS = {"high": "/stream1", "low": "/stream2"}


class HomeCameraError(RuntimeError):
    """A safe, credential-free failure from the private camera workflow."""


@dataclass(frozen=True, repr=False)
class HomeCameraConfig:
    camera_ip: str
    rtsp_port: int
    username: str
    password: str
    ssh_alias: str
    expected_hostname: str


@dataclass(frozen=True)
class HomeCameraCapture:
    mode: Literal["photo", "clip"]
    stream: Literal["high", "low"]
    captured_at: str
    output: str
    width: int
    height: int
    duration: float | None = None


@dataclass(frozen=True)
class _OutputReservation:
    """Private staging file held inside a trusted output directory."""

    directory_fd: int
    final_name: str
    staging_name: str
    requested_path: Path
    overwrite: bool

    @property
    def staging_path(self) -> Path:
        # Keep capture and ffprobe anchored to the opened directory inode, even
        # if an untrusted ancestor directory is renamed concurrently.
        return Path(f"/proc/self/fd/{self.directory_fd}/{self.staging_name}")


def _check_root_only_stat(info: os.stat_result, expected_mode: int, label: str) -> None:
    """Validate an already-opened object so checks and reads share one inode."""
    if not stat.S_ISDIR(info.st_mode) and label == "config_directory":
        raise HomeCameraError(f"{label}_not_directory")
    if not stat.S_ISREG(info.st_mode) and label == "config_file":
        raise HomeCameraError(f"{label}_not_regular")
    if info.st_uid != 0:
        raise HomeCameraError(f"{label}_not_root_owned")
    if stat.S_IMODE(info.st_mode) != expected_mode:
        raise HomeCameraError(f"{label}_permissions_invalid")


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> HomeCameraConfig:
    """Load and validate the root-only runtime configuration."""
    if path.name in {"", ".", ".."}:
        raise HomeCameraError("config_file_missing")
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    try:
        directory_fd = os.open(path.parent, directory_flags)
    except OSError as exc:
        raise HomeCameraError("config_directory_missing") from exc
    try:
        _check_root_only_stat(
            os.fstat(directory_fd),
            _EXPECTED_CONFIG_DIRECTORY_MODE,
            "config_directory",
        )
        try:
            file_fd = os.open(
                path.name,
                os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=directory_fd,
            )
        except OSError as exc:
            raise HomeCameraError("config_file_missing") from exc
        try:
            _check_root_only_stat(os.fstat(file_fd), _EXPECTED_CONFIG_MODE, "config_file")
            with os.fdopen(file_fd, encoding="utf-8") as stream:
                file_fd = -1
                payload = json.load(stream)
        finally:
            if file_fd >= 0:
                os.close(file_fd)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise HomeCameraError("config_unreadable") from exc
    finally:
        os.close(directory_fd)
    if not isinstance(payload, dict):
        raise HomeCameraError("config_invalid")
    try:
        config = HomeCameraConfig(
            camera_ip=str(payload["camera_ip"]),
            rtsp_port=int(payload["rtsp_port"]),
            username=str(payload["username"]),
            password=str(payload["password"]),
            ssh_alias=str(payload["ssh_alias"]),
            expected_hostname=str(payload["expected_hostname"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise HomeCameraError("config_invalid") from exc
    validate_config(config)
    return config


def validate_config(config: HomeCameraConfig) -> None:
    """Reject unsafe targets and values that could change SSH semantics."""
    try:
        camera_ip = ipaddress.ip_address(config.camera_ip)
    except ValueError as exc:
        raise HomeCameraError("camera_ip_invalid") from exc
    if not isinstance(camera_ip, ipaddress.IPv4Address) or not camera_ip.is_private:
        raise HomeCameraError("camera_ip_not_private_ipv4")
    if config.rtsp_port != 554:
        raise HomeCameraError("rtsp_port_invalid")
    if config.ssh_alias != "home-pc":
        raise HomeCameraError("ssh_alias_invalid")
    if config.expected_hostname != "DESKTOP-BUSO4I8":
        raise HomeCameraError("home_pc_identity_invalid")
    if not config.username or not config.password or "\x00" in config.username or "\x00" in config.password:
        raise HomeCameraError("camera_credentials_invalid")


def build_rtsp_uri(config: HomeCameraConfig, local_port: int, stream: Literal["high", "low"]) -> str:
    """Build the in-memory URI with credentials encoded as URI userinfo."""
    if stream not in _STREAM_PATHS:
        raise ValueError("stream must be high or low")
    if not 1 <= local_port <= 65535:
        raise ValueError("local_port must be between 1 and 65535")
    username = quote(config.username, safe="")
    password = quote(config.password, safe="")
    return f"rtsp://{username}:{password}@127.0.0.1:{local_port}{_STREAM_PATHS[stream]}"


def _reserve_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _ssh_argv(config: HomeCameraConfig, local_port: int) -> list[str]:
    return [
        "ssh",
        "-o",
        "ControlMaster=no",
        "-o",
        "ControlPath=none",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "ExitOnForwardFailure=yes",
        "-o",
        "ServerAliveInterval=15",
        "-o",
        "ServerAliveCountMax=2",
        "-L",
        f"127.0.0.1:{local_port}:{config.camera_ip}:{config.rtsp_port}",
        "-N",
        config.ssh_alias,
    ]


def _wait_for_forward(process: subprocess.Popen[bytes], local_port: int, timeout: float = 8.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise HomeCameraError("ssh_forward_failed")
        try:
            with socket.create_connection(("127.0.0.1", local_port), timeout=0.3):
                return
        except OSError:
            time.sleep(0.1)
    raise HomeCameraError("ssh_forward_timeout")


@contextmanager
def temporary_ssh_forward(config: HomeCameraConfig) -> Iterator[int]:
    """Expose the camera only on a temporary server-local TCP listener."""
    local_port = _reserve_local_port()
    process = subprocess.Popen(
        _ssh_argv(config, local_port),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        _wait_for_forward(process, local_port)
        yield local_port
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        if process.stderr is not None:
            process.stderr.close()


def classify_camera_backend_error(message: str, *, timed_out: bool = False) -> str:
    """Map backend diagnostics to safe codes without returning URI-bearing text."""
    if timed_out:
        return "camera_timeout"
    lowered = message.casefold()
    if "401" in lowered or "unauthorized" in lowered:
        return "camera_authentication_failed"
    if "connection refused" in lowered:
        return "camera_connection_refused"
    if "network is unreachable" in lowered or "no route to host" in lowered:
        return "camera_network_unreachable"
    if "timed out" in lowered or "timeout" in lowered:
        return "camera_timeout"
    return "camera_capture_failed"


def _open_private_output_directory(path: Path) -> int:
    """Open a root-private output directory without following its final link."""
    if path.name in {"", ".", ".."}:
        raise HomeCameraError("output_path_invalid")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    try:
        directory_fd = os.open(path.parent, flags)
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"output directory does not exist: {path.parent}") from exc
    except NotADirectoryError as exc:
        try:
            is_link = stat.S_ISLNK(path.parent.lstat().st_mode)
        except OSError:
            is_link = False
        if is_link:
            raise HomeCameraError("output_directory_not_real") from exc
        raise HomeCameraError("output_directory_not_directory") from exc
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise HomeCameraError("output_directory_not_real") from exc
        raise HomeCameraError("output_directory_unavailable") from exc

    try:
        info = os.fstat(directory_fd)
        if not stat.S_ISDIR(info.st_mode):  # Defensive: O_DIRECTORY already enforces this on Linux.
            raise HomeCameraError("output_directory_not_directory")
        if info.st_uid != 0:
            raise HomeCameraError("output_directory_not_root_owned")
        if stat.S_IMODE(info.st_mode) != _EXPECTED_OUTPUT_DIRECTORY_MODE:
            raise HomeCameraError("output_directory_permissions_invalid")
    except BaseException:
        os.close(directory_fd)
        raise
    return directory_fd


def _entry_exists(directory_fd: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def _create_staging_file(directory_fd: int) -> str:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW
    for _ in range(100):
        name = f".autostop-camera-{secrets.token_hex(16)}.partial"
        try:
            file_fd = os.open(name, flags, 0o600, dir_fd=directory_fd)
        except FileExistsError:
            continue
        try:
            os.fchmod(file_fd, 0o600)
        finally:
            os.close(file_fd)
        return name
    raise HomeCameraError("output_staging_unavailable")


def _reserve_output(path: Path, *, overwrite: bool) -> _OutputReservation:
    """Create only a private staging file; never open the final leaf for writing."""
    directory_fd = _open_private_output_directory(path)
    try:
        if not overwrite and _entry_exists(directory_fd, path.name):
            raise FileExistsError(f"refusing to overwrite existing file: {path}")
        staging_name = _create_staging_file(directory_fd)
    except BaseException:
        os.close(directory_fd)
        raise
    return _OutputReservation(directory_fd, path.name, staging_name, path, overwrite)


def _validate_staging_file(reservation: _OutputReservation) -> None:
    try:
        info = os.stat(
            reservation.staging_name,
            dir_fd=reservation.directory_fd,
            follow_symlinks=False,
        )
    except FileNotFoundError as exc:
        raise HomeCameraError("camera_output_invalid") from exc
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0:
        raise HomeCameraError("camera_output_invalid")
    os.chmod(reservation.staging_name, 0o600, dir_fd=reservation.directory_fd)


def _publish_output(reservation: _OutputReservation) -> None:
    """Atomically expose the validated staged artifact at the requested leaf."""
    _validate_staging_file(reservation)
    if reservation.overwrite:
        os.replace(
            reservation.staging_name,
            reservation.final_name,
            src_dir_fd=reservation.directory_fd,
            dst_dir_fd=reservation.directory_fd,
        )
        return
    try:
        os.link(
            reservation.staging_name,
            reservation.final_name,
            src_dir_fd=reservation.directory_fd,
            dst_dir_fd=reservation.directory_fd,
            follow_symlinks=False,
        )
    except FileExistsError as exc:
        raise FileExistsError(f"refusing to overwrite existing file: {reservation.requested_path}") from exc
    os.unlink(reservation.staging_name, dir_fd=reservation.directory_fd)


def _discard_staging_output(reservation: _OutputReservation) -> None:
    with suppress(FileNotFoundError):
        os.unlink(reservation.staging_name, dir_fd=reservation.directory_fd)


def _capture_rtsp(uri: str, output_path: Path, *, mode: Literal["photo", "clip"], duration: int) -> None:
    try:
        import av
    except ImportError as exc:  # pragma: no cover - required production dependency.
        raise HomeCameraError("camera_runtime_missing") from exc

    av.logging.set_level(av.logging.PANIC)
    try:
        with av.open(
            uri,
            mode="r",
            options={"rtsp_transport": "tcp", "stimeout": "8000000"},
        ) as source:
            video: Any = next((stream for stream in source.streams if stream.type == "video"), None)
            if video is None or video.codec_context.name not in {"h264", "hevc"}:
                raise HomeCameraError("camera_video_stream_invalid")
            if mode == "photo":
                frame: Any = next(source.decode(video), None)
                if frame is None:
                    raise HomeCameraError("camera_frame_missing")
                with av.open(str(output_path), mode="w", format="image2") as target:
                    output_stream: Any = target.add_stream("mjpeg", rate=1)
                    output_stream.width = frame.width
                    output_stream.height = frame.height
                    output_stream.pix_fmt = "yuvj420p"
                    for packet in output_stream.encode(frame):
                        target.mux(packet)
                    for packet in output_stream.encode():
                        target.mux(packet)
            else:
                _encode_clip(source, video, output_path, duration)
    except HomeCameraError:
        raise
    except (TimeoutError, subprocess.TimeoutExpired) as exc:
        raise HomeCameraError("camera_timeout") from exc
    except Exception as exc:
        raise HomeCameraError(classify_camera_backend_error(str(exc))) from exc


def _encode_clip(source: Any, video: Any, output_path: Path, duration: int) -> None:
    """Encode one bounded video-only MP4 with fresh monotonic timestamps."""
    import av

    rate = video.average_rate or Fraction(15, 1)
    time_base = Fraction(rate.denominator, rate.numerator)
    started_at = time.monotonic()
    frame_count = 0
    with av.open(str(output_path), mode="w", format="mp4", options={"movflags": "+faststart"}) as target:
        output_stream = target.add_stream("libx264", rate=rate, options={"preset": "ultrafast", "crf": "23"})
        output_stream.width = video.codec_context.width
        output_stream.height = video.codec_context.height
        output_stream.pix_fmt = "yuv420p"
        output_stream.time_base = time_base
        for frame in source.decode(video):
            encoded_duration = frame_count * rate.denominator / rate.numerator
            if time.monotonic() - started_at >= duration or encoded_duration >= duration:
                break
            frame.pts = frame_count
            frame.time_base = time_base
            for packet in output_stream.encode(frame):
                target.mux(packet)
            frame_count += 1
        for packet in output_stream.encode():
            target.mux(packet)
    if frame_count == 0:
        raise HomeCameraError("camera_clip_missing")


def _ffprobe_argv(path: Path) -> list[str]:
    return [
        "/usr/bin/ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=codec_name,width,height:format=duration",
        "-of",
        "json",
        str(path),
    ]


def _probe_output(
    path: Path,
    mode: Literal["photo", "clip"],
    *,
    directory_fd: int | None = None,
) -> tuple[int, int, float | None]:
    run_kwargs: dict[str, Any] = {}
    if directory_fd is not None:
        # ``path`` is anchored at /proc/self/fd/<directory_fd>; inherit that
        # descriptor so ffprobe resolves the same opened directory inode.
        run_kwargs["pass_fds"] = (directory_fd,)
    result = subprocess.run(
        _ffprobe_argv(path),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=10,
        check=False,
        **run_kwargs,
    )
    if result.returncode != 0:
        raise HomeCameraError("camera_output_invalid")
    try:
        payload = json.loads(result.stdout)
        stream = payload["streams"][0]
        width = int(stream["width"])
        height = int(stream["height"])
        codec = str(stream["codec_name"])
        duration = float(payload.get("format", {}).get("duration", 0)) if mode == "clip" else None
    except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise HomeCameraError("camera_output_invalid") from exc
    if width <= 0 or height <= 0 or (mode == "photo" and codec != "mjpeg"):
        raise HomeCameraError("camera_output_invalid")
    if mode == "photo":
        content = path.read_bytes()
        if not content.startswith(b"\xff\xd8") or not content.endswith(b"\xff\xd9"):
            raise HomeCameraError("camera_output_invalid")
    elif duration is None or duration <= 0:
        raise HomeCameraError("camera_output_invalid")
    return width, height, duration


def capture_home_camera(
    output_path: Path,
    *,
    mode: Literal["photo", "clip"] = "photo",
    stream: Literal["high", "low"] = "high",
    duration: int = 10,
    overwrite: bool = False,
    config_path: Path = DEFAULT_CONFIG_PATH,
) -> HomeCameraCapture:
    """Capture only on an explicit call; no polling, PTZ, audio, or recognition."""
    if mode not in {"photo", "clip"}:
        raise ValueError("mode must be photo or clip")
    if stream not in _STREAM_PATHS:
        raise ValueError("stream must be high or low")
    if not 1 <= duration <= 30:
        raise ValueError("duration must be between 1 and 30 seconds")
    config = load_config(config_path)
    reservation = _reserve_output(output_path, overwrite=overwrite)
    selected_stream = stream
    published = False
    try:
        with temporary_ssh_forward(config) as local_port:
            try:
                uri = build_rtsp_uri(config, local_port, selected_stream)
                _capture_rtsp(uri, reservation.staging_path, mode=mode, duration=duration)
            except HomeCameraError as exc:
                if mode != "photo" or stream != "high" or str(exc) == "camera_authentication_failed":
                    raise
                selected_stream = "low"
                uri = build_rtsp_uri(config, local_port, selected_stream)
                _capture_rtsp(uri, reservation.staging_path, mode=mode, duration=duration)
        width, height, actual_duration = _probe_output(
            reservation.staging_path,
            mode,
            directory_fd=reservation.directory_fd,
        )
        _publish_output(reservation)
        published = True
    finally:
        try:
            if not published:
                _discard_staging_output(reservation)
        finally:
            os.close(reservation.directory_fd)
    return HomeCameraCapture(
        mode=mode,
        stream=selected_stream,
        captured_at=datetime.now(UTC).isoformat(),
        output=str(output_path),
        width=width,
        height=height,
        duration=actual_duration,
    )


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Capture from the owner-authorized private home camera.")
    parser.add_argument("--mode", choices=("photo", "clip"), default="photo")
    parser.add_argument("--stream", choices=("high", "low"), default="high")
    parser.add_argument("--duration", type=int, default=10, help="Clip duration, 1 to 30 seconds.")
    parser.add_argument("--output", required=True, type=Path, help="Exact output path; parent must exist.")
    parser.add_argument("--overwrite", action="store_true", help="Allow replacing the exact output file.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        result = capture_home_camera(
            args.output,
            mode=args.mode,
            stream=args.stream,
            duration=args.duration,
            overwrite=args.overwrite,
            config_path=args.config,
        )
    except HomeCameraError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 2
    print(json.dumps({"ok": True, **asdict(result)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
