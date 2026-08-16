from __future__ import annotations

from contextlib import contextmanager
import json
from types import SimpleNamespace

from defusedxml import ElementTree
import pytest

from autostop_manager import home_camera_ptz
from autostop_manager.home_camera import HomeCameraConfig
from autostop_manager.home_camera_ptz import (
    HomeCameraPTZError,
    PTZController,
    PTZPosition,
    _PTZProfile,
    _PTZSpace,
)


def _config() -> HomeCameraConfig:
    return HomeCameraConfig(
        camera_ip="192.168.50.23",
        rtsp_port=554,
        username="camera-user<&",
        password="camera-password<&",
        ssh_alias="home-pc",
        expected_hostname="DESKTOP-BUSO4I8",
    )


def _xml(payload: str):
    return ElementTree.fromstring(payload)


CAPABILITIES = _xml(
    """<Envelope><Body><GetCapabilitiesResponse><Capabilities>
    <Media><XAddr>http://camera.invalid:2020/onvif/media_service</XAddr></Media>
    <PTZ><XAddr>http://camera.invalid:2020/onvif/ptz_service</XAddr></PTZ>
    </Capabilities></GetCapabilitiesResponse></Body></Envelope>"""
)
PROFILES = _xml(
    """<Envelope><Body><GetProfilesResponse>
    <Profiles token="profile-token"><PTZConfiguration token="config-token"/></Profiles>
    </GetProfilesResponse></Body></Envelope>"""
)
OPTIONS = _xml(
    """<Envelope><Body><GetConfigurationOptionsResponse><PTZConfigurationOptions><Spaces>
    <RelativePanTiltTranslationSpace><URI>relative-space</URI><XRange><Min>-1</Min><Max>1</Max></XRange><YRange><Min>-1</Min><Max>1</Max></YRange></RelativePanTiltTranslationSpace>
    <AbsolutePanTiltPositionSpace><URI>absolute-space</URI><XRange><Min>-1</Min><Max>1</Max></XRange><YRange><Min>-1</Min><Max>1</Max></YRange></AbsolutePanTiltPositionSpace>
    <ContinuousPanTiltVelocitySpace><URI>continuous-space</URI><XRange><Min>-1</Min><Max>1</Max></XRange><YRange><Min>-1</Min><Max>1</Max></YRange></ContinuousPanTiltVelocitySpace>
    </Spaces></PTZConfigurationOptions></GetConfigurationOptionsResponse></Body></Envelope>"""
)


def _status(pan: float = 0.25, tilt: float = -0.5):
    return _xml(
        f"<Envelope><Body><GetStatusResponse><PTZStatus><Position>"
        f'<PanTilt x="{pan}" y="{tilt}"/>'
        "</Position></PTZStatus></GetStatusResponse></Body></Envelope>"
    )


class FakeController(PTZController):
    def __init__(self):
        super().__init__(_config(), 45554)
        self.calls: list[tuple[str, str]] = []
        self.status_responses = [_status()]

    def _call(self, path: str, body: str):
        self.calls.append((path, body))
        if "GetCapabilities" in body:
            return CAPABILITIES
        if "GetProfiles" in body:
            return PROFILES
        if "GetConfigurationOptions" in body:
            return OPTIONS
        if "GetStatus" in body:
            return self.status_responses.pop(0) if self.status_responses else _status()
        return _xml("<Envelope><Body><Response/></Body></Envelope>")


def _profile(*, relative: bool = True, absolute: bool = True) -> _PTZProfile:
    space = _PTZSpace("space", -1.0, 1.0, -1.0, 1.0)
    return _PTZProfile(
        profile_token="profile-token",
        ptz_path="/onvif/ptz_service",
        relative=space if relative else None,
        absolute=space if absolute else None,
        continuous=space,
    )


def test_safe_service_path_accepts_only_onvif_http_paths():
    assert home_camera_ptz._safe_service_path("http://camera.invalid:2020/onvif/ptz_service") == ("/onvif/ptz_service")
    for value in (None, "file:///onvif/ptz_service", "http://camera.invalid/private"):
        with pytest.raises(HomeCameraPTZError, match="ptz_service"):
            home_camera_ptz._safe_service_path(value)


def test_ptz_forward_argv_is_loopback_only_strict_and_secret_free():
    config = _config()
    argv = home_camera_ptz._ptz_ssh_argv(config, 45554)
    rendered = " ".join(argv)

    assert "127.0.0.1:45554:192.168.50.23:2020" in argv
    assert "StrictHostKeyChecking=yes" in argv
    assert "ControlMaster=no" in argv
    assert config.username not in rendered
    assert config.password not in rendered


def test_wsse_envelope_escapes_username_and_never_contains_plain_password():
    controller = PTZController(_config(), 45554)
    payload = controller._envelope("<tds:GetCapabilities/>").decode()

    assert "camera-user&lt;&amp;" in payload
    assert _config().password not in payload
    assert "PasswordDigest" in payload


def test_discovery_and_status_return_only_safe_capabilities():
    controller = FakeController()

    result = controller.status()

    assert result.position == PTZPosition(0.25, -0.5)
    assert result.relative_supported is True
    assert result.absolute_supported is True
    assert result.continuous_supported is True
    assert controller._profile is not None
    assert controller._profile.ptz_path == "/onvif/ptz_service"


def test_move_relative_is_bounded_and_uses_relative_space(monkeypatch):
    controller = FakeController()
    monkeypatch.setattr(controller, "_wait_for_position", lambda *_args, **_kwargs: PTZPosition(0.30, -0.5))

    result = controller.move_relative("right", "small")

    assert result.before == PTZPosition(0.25, -0.5)
    assert result.after == PTZPosition(0.30, -0.5)
    move_body = next(body for _path, body in controller.calls if "RelativeMove" in body)
    assert 'x="0.05"' in move_body
    assert 'y="0.0"' in move_body
    assert "camera-password" not in move_body


@pytest.mark.parametrize("direction", ["left", "right", "up", "down"])
def test_all_allowlisted_directions_are_supported(monkeypatch, direction):
    controller = FakeController()
    monkeypatch.setattr(controller, "_wait_for_position", lambda *_args, **_kwargs: PTZPosition(0.0, 0.0))

    assert controller.move_relative(direction, "small").direction == direction


def test_move_rejects_unlisted_direction_and_step():
    controller = FakeController()
    with pytest.raises(ValueError, match="direction"):
        controller.move_relative("diagonal", "small")
    with pytest.raises(ValueError, match="step"):
        controller.move_relative("left", "large")


def test_move_rejects_unsupported_or_out_of_range_target(monkeypatch):
    controller = FakeController()
    monkeypatch.setattr(controller, "_discover", lambda: _profile(relative=False))
    with pytest.raises(HomeCameraPTZError, match="relative_not_supported"):
        controller.move_relative("left", "small")

    controller = FakeController()
    controller.status_responses = [_status(0.98, 0.0)]
    with pytest.raises(HomeCameraPTZError, match="target_out_of_range"):
        controller.move_relative("right", "small")


def test_move_timeout_sends_stop(monkeypatch):
    controller = FakeController()
    stopped = False

    def fail(*_args, **_kwargs):
        raise HomeCameraPTZError("ptz_move_timeout")

    def stop():
        nonlocal stopped
        stopped = True

    monkeypatch.setattr(controller, "_wait_for_position", fail)
    monkeypatch.setattr(controller, "stop", stop)

    with pytest.raises(HomeCameraPTZError, match="ptz_move_timeout"):
        controller.move_relative("left", "small")
    assert stopped is True


def test_goto_restores_a_previously_read_position(monkeypatch):
    controller = FakeController()
    target = PTZPosition(0.1, -0.2)
    monkeypatch.setattr(controller, "_wait_for_position", lambda *_args, **_kwargs: target)

    assert controller.goto(target) == target
    body = next(body for _path, body in controller.calls if "AbsoluteMove" in body)
    assert 'x="0.1"' in body
    assert 'y="-0.2"' in body


def test_goto_rejects_missing_absolute_support_or_invalid_target(monkeypatch):
    controller = FakeController()
    monkeypatch.setattr(controller, "_discover", lambda: _profile(absolute=False))
    with pytest.raises(HomeCameraPTZError, match="absolute_not_supported"):
        controller.goto(PTZPosition(0.0, 0.0))

    controller = FakeController()
    with pytest.raises(HomeCameraPTZError, match="target_out_of_range"):
        controller.goto(PTZPosition(2.0, 0.0))


def test_stop_uses_both_pan_tilt_and_zoom_flags():
    controller = FakeController()

    controller.stop()

    body = next(body for _path, body in controller.calls if "<tptz:Stop>" in body)
    assert "<tptz:PanTilt>true" in body
    assert "<tptz:Zoom>true" in body


def test_temporary_forward_always_terminates_process(monkeypatch):
    class Process:
        terminated = False

        def poll(self):
            return None

        def terminate(self):
            self.terminated = True

        def wait(self, timeout):
            return 0

    process = Process()
    monkeypatch.setattr(home_camera_ptz, "_reserve_local_port", lambda: 45554)
    monkeypatch.setattr(home_camera_ptz, "_wait_for_forward", lambda *_args: None)
    monkeypatch.setattr(home_camera_ptz.subprocess, "Popen", lambda *_args, **_kwargs: process)

    with home_camera_ptz.temporary_onvif_forward(_config()) as port:
        assert port == 45554

    assert process.terminated is True


def test_exclusive_lock_rejects_a_second_controller(tmp_path):
    lock_path = tmp_path / "ptz.lock"

    with home_camera_ptz._exclusive_ptz_lock(lock_path):
        with pytest.raises(HomeCameraPTZError, match="ptz_busy"):
            with home_camera_ptz._exclusive_ptz_lock(lock_path):
                pass

    assert lock_path.stat().st_mode & 0o777 == 0o600


def test_http_errors_are_mapped_to_safe_codes(monkeypatch):
    class Response:
        status = 401

        def getheader(self, _name):
            return None

        def read(self, _size=None):
            return b"credential-bearing raw body"

    class Connection:
        def __init__(self, *_args, **_kwargs):
            pass

        def request(self, *_args, **_kwargs):
            pass

        def getresponse(self):
            return Response()

        def close(self):
            pass

    monkeypatch.setattr(home_camera_ptz.http.client, "HTTPConnection", Connection)

    with pytest.raises(HomeCameraPTZError, match="ptz_authentication_failed") as error:
        PTZController(_config(), 45554)._call("/onvif/device_service", "<body/>")
    assert "credential" not in str(error.value)


def test_http_response_without_length_is_still_bounded(monkeypatch):
    class Response:
        status = 200

        def getheader(self, _name):
            return None

        def read(self, size):
            return b"x" * size

    class Connection:
        def __init__(self, *_args, **_kwargs):
            pass

        def request(self, *_args, **_kwargs):
            pass

        def getresponse(self):
            return Response()

        def close(self):
            pass

    monkeypatch.setattr(home_camera_ptz.http.client, "HTTPConnection", Connection)

    with pytest.raises(HomeCameraPTZError, match="ptz_response_too_large"):
        PTZController(_config(), 45554)._call("/onvif/device_service", "<body/>")


def test_idempotent_read_retries_one_transient_timeout(monkeypatch):
    controller = PTZController(_config(), 45554)
    calls = 0

    def read(_path, _body):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise HomeCameraPTZError("ptz_timeout")
        return _xml("<Envelope><Body><Response/></Body></Envelope>")

    monkeypatch.setattr(controller, "_call", read)
    monkeypatch.setattr(home_camera_ptz.time, "sleep", lambda _seconds: None)

    assert home_camera_ptz._local_name(controller._read_call("/onvif/read", "<Get/>")) == "Envelope"
    assert calls == 2


def test_mutating_timeout_uses_best_effort_stop(monkeypatch):
    controller = FakeController()
    stopped = False

    def call(path, body):
        if "RelativeMove" in body:
            raise HomeCameraPTZError("ptz_timeout")
        return FakeController._call(controller, path, body)

    def stop():
        nonlocal stopped
        stopped = True

    monkeypatch.setattr(controller, "_call", call)
    monkeypatch.setattr(controller, "stop", stop)

    with pytest.raises(HomeCameraPTZError, match="ptz_timeout"):
        controller.move_relative("left", "small")
    assert stopped is True


def test_main_returns_safe_json(monkeypatch, capsys):
    controller = SimpleNamespace(
        status=lambda: home_camera_ptz.PTZStatus(
            "now",
            PTZPosition(0.1, -0.2),
            True,
            True,
            True,
        )
    )

    @contextmanager
    def opened(*_args, **_kwargs):
        yield controller

    monkeypatch.setattr(home_camera_ptz, "open_ptz_controller", opened)
    assert home_camera_ptz.main(["--action", "status"]) == 0
    assert json.loads(capsys.readouterr().out)["position"] == {"pan": 0.1, "tilt": -0.2}

    @contextmanager
    def failed(*_args, **_kwargs):
        raise HomeCameraPTZError("ptz_timeout")
        yield

    monkeypatch.setattr(home_camera_ptz, "open_ptz_controller", failed)
    assert home_camera_ptz.main(["--action", "status"]) == 2
    assert json.loads(capsys.readouterr().out) == {"ok": False, "error": "ptz_timeout"}


def test_main_goto_restores_explicit_coordinates(monkeypatch, capsys):
    restored: list[PTZPosition] = []
    controller = SimpleNamespace(goto=lambda position: restored.append(position) or position)

    @contextmanager
    def opened(*_args, **_kwargs):
        yield controller

    monkeypatch.setattr(home_camera_ptz, "open_ptz_controller", opened)
    assert home_camera_ptz.main(["--action", "goto", "--pan", "0.2", "--tilt", "-0.4"]) == 0
    assert restored == [PTZPosition(0.2, -0.4)]
    assert json.loads(capsys.readouterr().out)["position"] == {"pan": 0.2, "tilt": -0.4}
