from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path

import pytest

from autostop_manager import home_camera
from autostop_manager.home_camera import HomeCameraConfig, HomeCameraError


def _config() -> HomeCameraConfig:
    return HomeCameraConfig(
        camera_ip="192.168.0.107",
        rtsp_port=554,
        username="owner+camera@example.test",
        password="p@ss:/?#[] value",
        ssh_alias="home-pc",
        expected_hostname="DESKTOP-BUSO4I8",
    )


def _write_config(path: Path, *, mode: int = 0o600) -> None:
    path.parent.mkdir(mode=0o700)
    path.parent.chmod(0o700)
    path.write_text(
        json.dumps(
            {
                "camera_ip": "192.168.0.107",
                "rtsp_port": 554,
                "username": "camera-user",
                "password": "camera-password",
                "ssh_alias": "home-pc",
                "expected_hostname": "DESKTOP-BUSO4I8",
            }
        ),
        encoding="utf-8",
    )
    path.chmod(mode)


def test_load_config_requires_root_only_permissions(tmp_path):
    config_path = tmp_path / "private" / "camera.json"
    _write_config(config_path, mode=0o644)

    with pytest.raises(HomeCameraError, match="config_file_permissions_invalid"):
        home_camera.load_config(config_path)


@pytest.mark.parametrize("camera_ip", ["8.8.8.8", "2001:db8::1", "not-an-ip"])
def test_validate_config_rejects_non_private_ipv4(camera_ip):
    config = _config()
    config = HomeCameraConfig(
        camera_ip=camera_ip,
        rtsp_port=config.rtsp_port,
        username=config.username,
        password=config.password,
        ssh_alias=config.ssh_alias,
        expected_hostname=config.expected_hostname,
    )

    with pytest.raises(HomeCameraError, match="camera_ip"):
        home_camera.validate_config(config)


def test_rtsp_credentials_are_encoded_and_ffmpeg_argv_is_secret_free(tmp_path):
    config = _config()
    uri = home_camera.build_rtsp_uri(config, 45554, "high")
    argv = home_camera.build_ffmpeg_argv(
        memfd=17,
        output_path=tmp_path / "frame.jpg",
        mode="photo",
        duration=10,
    )

    assert config.username not in uri
    assert config.password not in uri
    assert "owner%2Bcamera%40example.test" in uri
    assert "p%40ss%3A%2F%3F%23%5B%5D%20value" in uri
    assert uri not in argv
    assert config.username not in " ".join(argv)
    assert config.password not in " ".join(argv)
    assert argv[argv.index("-i") + 1] == "/proc/self/fd/17"


def test_ssh_forward_is_local_and_strict():
    argv = home_camera._ssh_argv(_config(), 45554)

    assert "ControlMaster=no" in argv
    assert "ControlPath=none" in argv
    assert "StrictHostKeyChecking=yes" in argv
    assert "ExitOnForwardFailure=yes" in argv
    assert "127.0.0.1:45554:192.168.0.107:554" in argv
    assert argv[-1] == "home-pc"


@pytest.mark.parametrize(
    "stderr, code",
    [
        (b"rtsp://secret:secret@127.0.0.1:1: Server returned 401 Unauthorized", "camera_authentication_failed"),
        (b"Connection refused", "camera_connection_refused"),
        (b"Network is unreachable", "camera_network_unreachable"),
        (b"rtsp://secret:secret@127.0.0.1:1: unknown failure", "camera_capture_failed"),
    ],
)
def test_ffmpeg_errors_are_safe_codes(stderr, code):
    result = home_camera.classify_ffmpeg_error(stderr)

    assert result == code
    assert "secret" not in result
    assert "rtsp" not in result


def test_capture_retries_low_once_on_technical_error_and_cleans_up(monkeypatch, tmp_path):
    calls: list[str] = []

    @contextmanager
    def fake_forward(_config):
        yield 45554

    def fake_run(uri, output_path, *, mode, duration):
        calls.append(uri)
        if uri.endswith("/stream1"):
            raise HomeCameraError("camera_timeout")
        output_path.write_bytes(b"\xff\xd8frame\xff\xd9")

    monkeypatch.setattr(home_camera, "load_config", lambda _path: _config())
    monkeypatch.setattr(home_camera, "temporary_ssh_forward", fake_forward)
    monkeypatch.setattr(home_camera, "_run_ffmpeg", fake_run)
    monkeypatch.setattr(home_camera, "_probe_output", lambda _path, _mode: (1280, 720, None))
    output = tmp_path / "frame.jpg"

    result = home_camera.capture_home_camera(output)

    assert result.stream == "low"
    assert len(calls) == 2
    assert stat_mode(output) == 0o600


def test_capture_does_not_retry_authentication_and_removes_partial(monkeypatch, tmp_path):
    calls = 0

    @contextmanager
    def fake_forward(_config):
        yield 45554

    def fake_run(_uri, output_path, *, mode, duration):
        nonlocal calls
        calls += 1
        output_path.write_bytes(b"partial")
        raise HomeCameraError("camera_authentication_failed")

    monkeypatch.setattr(home_camera, "load_config", lambda _path: _config())
    monkeypatch.setattr(home_camera, "temporary_ssh_forward", fake_forward)
    monkeypatch.setattr(home_camera, "_run_ffmpeg", fake_run)
    output = tmp_path / "frame.jpg"

    with pytest.raises(HomeCameraError, match="camera_authentication_failed"):
        home_camera.capture_home_camera(output)

    assert calls == 1
    assert not output.exists()


def test_capture_refuses_overwrite_before_starting_tunnel(monkeypatch, tmp_path):
    output = tmp_path / "frame.jpg"
    output.write_bytes(b"keep")
    monkeypatch.setattr(home_camera, "load_config", lambda _path: _config())

    with pytest.raises(FileExistsError):
        home_camera.capture_home_camera(output)

    assert output.read_bytes() == b"keep"


def test_clip_duration_is_bounded(tmp_path):
    with pytest.raises(ValueError, match="between 1 and 30"):
        home_camera.capture_home_camera(tmp_path / "clip.mp4", mode="clip", duration=31)


def stat_mode(path: Path) -> int:
    return os.stat(path).st_mode & 0o777
