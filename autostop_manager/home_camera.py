"""Owner-requested capture from the private home camera through ``home-pc``."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import ipaddress
import json
import os
from pathlib import Path
import socket
import stat
import subprocess
import time
from typing import Literal
from urllib.parse import quote


DEFAULT_CONFIG_PATH = Path("/etc/autostop-camera/home-tapo-c225.json")
_EXPECTED_CONFIG_DIRECTORY_MODE = 0o700
_EXPECTED_CONFIG_MODE = 0o600
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


def _check_root_only(path: Path, expected_mode: int, label: str) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise HomeCameraError(f"{label}_missing") from exc
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
    _check_root_only(path.parent, _EXPECTED_CONFIG_DIRECTORY_MODE, "config_directory")
    _check_root_only(path, _EXPECTED_CONFIG_MODE, "config_file")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HomeCameraError("config_unreadable") from exc
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


def _open_uri_memfd(uri: str) -> int:
    if not hasattr(os, "memfd_create"):
        raise HomeCameraError("memfd_unavailable")
    fd = os.memfd_create("autostop-camera-input", flags=os.MFD_CLOEXEC)
    try:
        descriptor = f"ffconcat version 1.0\nfile '{uri}'\n".encode()
        os.write(fd, descriptor)
        os.lseek(fd, 0, os.SEEK_SET)
        return fd
    except BaseException:
        os.close(fd)
        raise


def build_ffmpeg_argv(
    *,
    memfd: int,
    output_path: Path,
    mode: Literal["photo", "clip"],
    duration: int,
) -> list[str]:
    """Return a secret-free ffmpeg argv; the RTSP URI lives only in ``memfd``."""
    argv = [
        "/usr/bin/ffmpeg",
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "error",
        "-rtsp_transport",
        "tcp",
        "-timeout",
        "8000000",
        "-f",
        "concat",
        "-safe",
        "0",
        "-protocol_whitelist",
        "file,rtsp,tcp,udp",
        "-i",
        f"/proc/self/fd/{memfd}",
    ]
    if mode == "photo":
        argv.extend(["-frames:v", "1", "-an", "-c:v", "mjpeg"])
    elif mode == "clip":
        argv.extend(["-t", str(duration), "-map", "0:v:0", "-an", "-c:v", "copy", "-movflags", "+faststart"])
    else:
        raise ValueError("mode must be photo or clip")
    argv.extend(["-y", str(output_path)])
    return argv


def classify_ffmpeg_error(stderr: bytes, *, timed_out: bool = False) -> str:
    """Map ffmpeg diagnostics to safe codes without returning its URI-bearing text."""
    if timed_out:
        return "camera_timeout"
    lowered = stderr.lower()
    if b"401 unauthorized" in lowered or b"method describe failed: 401" in lowered:
        return "camera_authentication_failed"
    if b"connection refused" in lowered:
        return "camera_connection_refused"
    if b"network is unreachable" in lowered or b"no route to host" in lowered:
        return "camera_network_unreachable"
    if b"timed out" in lowered or b"connection timeout" in lowered:
        return "camera_timeout"
    return "camera_capture_failed"


def _reserve_output(path: Path, *, overwrite: bool) -> None:
    if not path.parent.is_dir():
        raise FileNotFoundError(f"output directory does not exist: {path.parent}")
    flags = os.O_WRONLY | os.O_CREAT
    flags |= os.O_TRUNC if overwrite else os.O_EXCL
    fd = os.open(path, flags, 0o600)
    os.close(fd)
    path.chmod(0o600)


def _run_ffmpeg(uri: str, output_path: Path, *, mode: Literal["photo", "clip"], duration: int) -> None:
    fd = _open_uri_memfd(uri)
    try:
        argv = build_ffmpeg_argv(memfd=fd, output_path=output_path, mode=mode, duration=duration)
        try:
            result = subprocess.run(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                pass_fds=(fd,),
                timeout=duration + 15,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise HomeCameraError(classify_ffmpeg_error(b"", timed_out=True)) from exc
        if result.returncode != 0:
            raise HomeCameraError(classify_ffmpeg_error(result.stderr))
    finally:
        os.close(fd)


def _probe_output(path: Path, mode: Literal["photo", "clip"]) -> tuple[int, int, float | None]:
    result = subprocess.run(
        [
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
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=10,
        check=False,
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
    _reserve_output(output_path, overwrite=overwrite)
    selected_stream = stream
    try:
        with temporary_ssh_forward(config) as local_port:
            try:
                uri = build_rtsp_uri(config, local_port, selected_stream)
                _run_ffmpeg(uri, output_path, mode=mode, duration=duration)
            except HomeCameraError as exc:
                if mode != "photo" or stream != "high" or str(exc) == "camera_authentication_failed":
                    raise
                selected_stream = "low"
                uri = build_rtsp_uri(config, local_port, selected_stream)
                _run_ffmpeg(uri, output_path, mode=mode, duration=duration)
        width, height, actual_duration = _probe_output(output_path, mode)
    except BaseException:
        output_path.unlink(missing_ok=True)
        raise
    output_path.chmod(0o600)
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
