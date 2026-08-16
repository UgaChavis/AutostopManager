from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from autostop_manager import home_camera
from autostop_manager.home_camera import HomeCameraConfig, HomeCameraError


def _config() -> HomeCameraConfig:
    return HomeCameraConfig(
        camera_ip="192.168.50.23",
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
                "camera_ip": "192.168.50.23",
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


def test_load_config_accepts_valid_root_only_file(tmp_path):
    config_path = tmp_path / "private" / "camera.json"
    _write_config(config_path)

    result = home_camera.load_config(config_path)

    assert result.camera_ip == "192.168.50.23"
    assert result.rtsp_port == 554


def test_load_config_rejects_symlink_leaf(tmp_path):
    real_config = tmp_path / "real" / "camera.json"
    _write_config(real_config)
    private_dir = tmp_path / "private"
    private_dir.mkdir(mode=0o700)
    config_path = private_dir / "camera.json"
    config_path.symlink_to(real_config)

    with pytest.raises(HomeCameraError, match="config_file_missing"):
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


@pytest.mark.parametrize(
    "field, value, code",
    [
        ("rtsp_port", 8554, "rtsp_port_invalid"),
        ("ssh_alias", "other", "ssh_alias_invalid"),
        ("expected_hostname", "OTHER", "home_pc_identity_invalid"),
        ("username", "", "camera_credentials_invalid"),
        ("password", "", "camera_credentials_invalid"),
    ],
)
def test_validate_config_rejects_unsafe_fields(field, value, code):
    values = _config().__dict__ | {field: value}

    with pytest.raises(HomeCameraError, match=code):
        home_camera.validate_config(HomeCameraConfig(**values))


def test_rtsp_credentials_are_encoded_and_process_argv_is_secret_free(tmp_path):
    config = _config()
    uri = home_camera.build_rtsp_uri(config, 45554, "high")
    argv = home_camera._ffprobe_argv(tmp_path / "frame.jpg")

    assert config.username not in uri
    assert config.password not in uri
    assert "owner%2Bcamera%40example.test" in uri
    assert "p%40ss%3A%2F%3F%23%5B%5D%20value" in uri
    assert uri not in argv
    assert config.username not in " ".join(argv)
    assert config.password not in " ".join(argv)
    assert argv[0] == "/usr/bin/ffprobe"


def test_rtsp_uri_rejects_invalid_stream_and_port():
    with pytest.raises(ValueError, match="stream"):
        home_camera.build_rtsp_uri(_config(), 45554, "other")
    with pytest.raises(ValueError, match="local_port"):
        home_camera.build_rtsp_uri(_config(), 0, "high")


def test_reserve_local_port_returns_loopback_port():
    assert 1 <= home_camera._reserve_local_port() <= 65535


def test_ssh_forward_is_local_and_strict():
    argv = home_camera._ssh_argv(_config(), 45554)

    assert "ControlMaster=no" in argv
    assert "ControlPath=none" in argv
    assert "StrictHostKeyChecking=yes" in argv
    assert "ExitOnForwardFailure=yes" in argv
    assert "127.0.0.1:45554:192.168.50.23:554" in argv
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
def test_camera_backend_errors_are_safe_codes(stderr, code):
    result = home_camera.classify_camera_backend_error(stderr.decode())

    assert result == code
    assert "secret" not in result
    assert "rtsp" not in result


def test_camera_backend_timeout_flag_is_safe():
    assert home_camera.classify_camera_backend_error("secret", timed_out=True) == "camera_timeout"


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
    monkeypatch.setattr(home_camera, "_capture_rtsp", fake_run)
    monkeypatch.setattr(home_camera, "_probe_output", lambda _path, _mode, **_kwargs: (1280, 720, None))
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
    monkeypatch.setattr(home_camera, "_capture_rtsp", fake_run)
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


def test_capture_rejects_invalid_mode_and_stream(tmp_path):
    with pytest.raises(ValueError, match="mode"):
        home_camera.capture_home_camera(tmp_path / "bad", mode="other")
    with pytest.raises(ValueError, match="stream"):
        home_camera.capture_home_camera(tmp_path / "bad", stream="other")


def test_reserve_output_requires_private_real_parent(tmp_path):
    with pytest.raises(FileNotFoundError):
        home_camera._reserve_output(tmp_path / "missing" / "frame.jpg", overwrite=False)

    trusted = tmp_path / "trusted"
    trusted.mkdir(mode=0o700)
    trusted.chmod(0o700)
    parent_link = tmp_path / "parent-link"
    parent_link.symlink_to(trusted, target_is_directory=True)

    with pytest.raises(HomeCameraError, match="output_directory_not_real"):
        home_camera._reserve_output(parent_link / "frame.jpg", overwrite=False)


def test_reserve_output_creates_private_staging_file(tmp_path):
    reservation = home_camera._reserve_output(tmp_path / "frame.jpg", overwrite=False)
    try:
        assert reservation.staging_path.exists()
        assert stat_mode(reservation.staging_path) == 0o600
    finally:
        home_camera._discard_staging_output(reservation)
        os.close(reservation.directory_fd)


def test_capture_overwrite_replaces_symlink_without_changing_its_target(monkeypatch, tmp_path):
    @contextmanager
    def fake_forward(_config):
        yield 45554

    def fake_run(_uri, output_path, *, mode, duration):
        output_path.write_bytes(b"new-frame")

    victim = tmp_path / "victim"
    victim.write_bytes(b"keep")
    output = tmp_path / "frame.jpg"
    output.symlink_to(victim.name)
    monkeypatch.setattr(home_camera, "load_config", lambda _path: _config())
    monkeypatch.setattr(home_camera, "temporary_ssh_forward", fake_forward)
    monkeypatch.setattr(home_camera, "_capture_rtsp", fake_run)
    monkeypatch.setattr(home_camera, "_probe_output", lambda _path, _mode, **_kwargs: (1280, 720, None))

    result = home_camera.capture_home_camera(output, overwrite=True)

    assert result.output == str(output)
    assert victim.read_bytes() == b"keep"
    assert not output.is_symlink()
    assert output.read_bytes() == b"new-frame"
    assert stat_mode(output) == 0o600


def test_capture_overwrite_replaces_hardlink_without_changing_sibling(monkeypatch, tmp_path):
    @contextmanager
    def fake_forward(_config):
        yield 45554

    def fake_run(_uri, output_path, *, mode, duration):
        output_path.write_bytes(b"new-frame")

    sibling = tmp_path / "sibling"
    sibling.write_bytes(b"keep")
    output = tmp_path / "frame.jpg"
    os.link(sibling, output)
    monkeypatch.setattr(home_camera, "load_config", lambda _path: _config())
    monkeypatch.setattr(home_camera, "temporary_ssh_forward", fake_forward)
    monkeypatch.setattr(home_camera, "_capture_rtsp", fake_run)
    monkeypatch.setattr(home_camera, "_probe_output", lambda _path, _mode, **_kwargs: (1280, 720, None))

    home_camera.capture_home_camera(output, overwrite=True)

    assert sibling.read_bytes() == b"keep"
    assert output.read_bytes() == b"new-frame"
    assert stat_mode(output) == 0o600


def test_capture_failure_preserves_existing_output_and_cleans_only_staging(monkeypatch, tmp_path):
    @contextmanager
    def fake_forward(_config):
        yield 45554

    def fake_run(_uri, output_path, *, mode, duration):
        output_path.write_bytes(b"partial")
        raise HomeCameraError("camera_authentication_failed")

    output = tmp_path / "frame.jpg"
    output.write_bytes(b"keep")
    monkeypatch.setattr(home_camera, "load_config", lambda _path: _config())
    monkeypatch.setattr(home_camera, "temporary_ssh_forward", fake_forward)
    monkeypatch.setattr(home_camera, "_capture_rtsp", fake_run)

    with pytest.raises(HomeCameraError, match="camera_authentication_failed"):
        home_camera.capture_home_camera(output, overwrite=True)

    assert output.read_bytes() == b"keep"
    assert not list(tmp_path.glob(".autostop-camera-*.partial"))


def test_capture_refuses_existing_symlink_before_starting_tunnel(monkeypatch, tmp_path):
    target = tmp_path / "target"
    target.write_bytes(b"keep")
    output = tmp_path / "frame.jpg"
    output.symlink_to(target.name)
    monkeypatch.setattr(home_camera, "load_config", lambda _path: _config())

    with pytest.raises(FileExistsError):
        home_camera.capture_home_camera(output)

    assert target.read_bytes() == b"keep"


def test_temporary_forward_terminates_process(monkeypatch):
    class FakePipe:
        closed = False

        def close(self):
            self.closed = True

    class FakeProcess:
        stderr = FakePipe()
        terminated = False

        def poll(self):
            return None

        def terminate(self):
            self.terminated = True

        def wait(self, timeout):
            return 0

    process = FakeProcess()
    monkeypatch.setattr(home_camera, "_reserve_local_port", lambda: 45554)
    monkeypatch.setattr(home_camera.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(home_camera, "_wait_for_forward", lambda _process, _port: None)

    with home_camera.temporary_ssh_forward(_config()) as port:
        assert port == 45554

    assert process.terminated is True
    assert process.stderr.closed is True


def test_wait_for_forward_reports_early_process_failure():
    process = SimpleNamespace(poll=lambda: 1)

    with pytest.raises(HomeCameraError, match="ssh_forward_failed"):
        home_camera._wait_for_forward(process, 45554, timeout=1)


class _FakePacket:
    pass


class _FakeFrame:
    width = 2688
    height = 1520
    pts = None
    time_base = None


class _FakeOutputStream:
    width = 0
    height = 0
    pix_fmt = ""
    time_base = None

    def encode(self, frame=None):
        return [_FakePacket()] if frame is not None else []


class _FakeTarget:
    def __init__(self):
        self.muxed = 0

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def add_stream(self, *args, **kwargs):
        return _FakeOutputStream()

    def mux(self, _packet):
        self.muxed += 1


class _FakeVideo:
    type = "video"
    average_rate = None
    codec_context = SimpleNamespace(name="h264", width=2688, height=1520)


class _FakeSource:
    def __init__(self):
        self.streams = [_FakeVideo()]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def decode(self, _video):
        return iter([_FakeFrame(), _FakeFrame()])


def _fake_av():
    source = _FakeSource()
    target = _FakeTarget()
    return SimpleNamespace(
        logging=SimpleNamespace(PANIC=0, set_level=lambda _level: None),
        open=lambda _path, **kwargs: source if kwargs.get("mode") == "r" else target,
    )


def test_capture_rtsp_photo_uses_in_memory_backend(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "av", _fake_av())

    home_camera._capture_rtsp("rtsp://credential-bearing", tmp_path / "frame.jpg", mode="photo", duration=10)


def test_capture_rtsp_maps_backend_error_without_uri(monkeypatch, tmp_path):
    backend = _fake_av()
    backend.open = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("401 Unauthorized secret URI"))
    monkeypatch.setitem(sys.modules, "av", backend)

    with pytest.raises(HomeCameraError, match="camera_authentication_failed"):
        home_camera._capture_rtsp("rtsp://credential-bearing", tmp_path / "frame.jpg", mode="photo", duration=10)


def test_encode_clip_writes_frames_with_fresh_timestamps(monkeypatch, tmp_path):
    backend = _fake_av()
    monkeypatch.setitem(sys.modules, "av", backend)
    ticks = iter([0.0, 0.1, 0.2])
    monkeypatch.setattr(home_camera.time, "monotonic", lambda: next(ticks))

    home_camera._encode_clip(_FakeSource(), _FakeVideo(), tmp_path / "clip.mp4", 1)


def test_probe_output_validates_photo_and_clip(monkeypatch, tmp_path):
    photo = tmp_path / "frame.jpg"
    photo.write_bytes(b"\xff\xd8frame\xff\xd9")
    photo_payload = {"streams": [{"codec_name": "mjpeg", "width": 2688, "height": 1520}], "format": {}}
    clip_payload = {
        "streams": [{"codec_name": "h264", "width": 2688, "height": 1520}],
        "format": {"duration": "3.0"},
    }
    results = iter(
        [
            SimpleNamespace(returncode=0, stdout=json.dumps(photo_payload).encode()),
            SimpleNamespace(returncode=0, stdout=json.dumps(clip_payload).encode()),
        ]
    )
    monkeypatch.setattr(home_camera.subprocess, "run", lambda *args, **kwargs: next(results))

    assert home_camera._probe_output(photo, "photo") == (2688, 1520, None)
    assert home_camera._probe_output(tmp_path / "clip.mp4", "clip") == (2688, 1520, 3.0)


def test_probe_output_rejects_ffprobe_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(
        home_camera.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout=b""),
    )

    with pytest.raises(HomeCameraError, match="camera_output_invalid"):
        home_camera._probe_output(tmp_path / "bad.jpg", "photo")


@pytest.mark.parametrize(
    "payload, mode, content",
    [
        (b"not-json", "photo", b"\xff\xd8x\xff\xd9"),
        (json.dumps({"streams": []}).encode(), "photo", b"\xff\xd8x\xff\xd9"),
        (
            json.dumps({"streams": [{"codec_name": "h264", "width": 0, "height": 1}]}).encode(),
            "photo",
            b"\xff\xd8x\xff\xd9",
        ),
        (
            json.dumps({"streams": [{"codec_name": "mjpeg", "width": 1, "height": 1}]}).encode(),
            "photo",
            b"not-jpeg",
        ),
        (
            json.dumps(
                {"streams": [{"codec_name": "h264", "width": 1, "height": 1}], "format": {"duration": "0"}}
            ).encode(),
            "clip",
            b"",
        ),
    ],
)
def test_probe_output_rejects_invalid_payload(monkeypatch, tmp_path, payload, mode, content):
    output = tmp_path / "output"
    output.write_bytes(content)
    monkeypatch.setattr(
        home_camera.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=payload),
    )

    with pytest.raises(HomeCameraError, match="camera_output_invalid"):
        home_camera._probe_output(output, mode)


def test_main_returns_safe_json_for_success_and_failure(monkeypatch, tmp_path, capsys):
    output = tmp_path / "frame.jpg"
    capture = home_camera.HomeCameraCapture("photo", "high", "now", str(output), 2688, 1520)
    monkeypatch.setattr(home_camera, "capture_home_camera", lambda *args, **kwargs: capture)

    assert home_camera.main(["--output", str(output)]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True

    def fail(*args, **kwargs):
        raise HomeCameraError("camera_timeout")

    monkeypatch.setattr(home_camera, "capture_home_camera", fail)
    assert home_camera.main(["--output", str(output)]) == 2
    assert json.loads(capsys.readouterr().out) == {"ok": False, "error": "camera_timeout"}


def stat_mode(path: Path) -> int:
    return os.stat(path).st_mode & 0o777
