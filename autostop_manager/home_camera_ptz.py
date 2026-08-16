"""Bounded owner-requested ONVIF PTZ control for the private home camera."""

from __future__ import annotations

import base64
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import fcntl
import hashlib
import http.client
import json
import math
import os
from pathlib import Path
import stat
import subprocess
import time
from typing import Literal
from urllib.parse import urlparse
from xml.sax.saxutils import escape, quoteattr

from defusedxml import ElementTree

from autostop_manager.home_camera import (
    DEFAULT_CONFIG_PATH,
    HomeCameraConfig,
    HomeCameraError,
    _reserve_local_port,
    _wait_for_forward,
    load_config,
)


ONVIF_PORT = 2020
DEFAULT_LOCK_PATH = Path("/run/lock/autostop-home-camera-ptz.lock")
_MAX_RESPONSE_BYTES = 1_000_000
_MOVE_TIMEOUT_SECONDS = 8.0
_POSITION_TOLERANCE = 0.002
_STEP_VALUES = {"small": 0.05, "medium": 0.10}
_DIRECTION_VECTORS = {
    "left": (-1.0, 0.0),
    "right": (1.0, 0.0),
    "up": (0.0, 1.0),
    "down": (0.0, -1.0),
}


class HomeCameraPTZError(HomeCameraError):
    """A safe, credential-free PTZ failure."""


@dataclass(frozen=True)
class PTZPosition:
    pan: float
    tilt: float


@dataclass(frozen=True)
class PTZStatus:
    captured_at: str
    position: PTZPosition
    relative_supported: bool
    absolute_supported: bool
    continuous_supported: bool


@dataclass(frozen=True)
class PTZMoveResult:
    direction: Literal["left", "right", "up", "down"]
    step: Literal["small", "medium"]
    before: PTZPosition
    after: PTZPosition
    completed_at: str


@dataclass(frozen=True)
class _PTZSpace:
    uri: str
    x_min: float
    x_max: float
    y_min: float
    y_max: float


@dataclass(frozen=True)
class _PTZProfile:
    profile_token: str
    ptz_path: str
    relative: _PTZSpace | None
    absolute: _PTZSpace | None
    continuous: _PTZSpace | None


def _local_name(element: ElementTree.Element) -> str:
    return element.tag.rsplit("}", 1)[-1]


def _first(root: ElementTree.Element, name: str) -> ElementTree.Element | None:
    return next((element for element in root.iter() if _local_name(element) == name), None)


def _child(root: ElementTree.Element, name: str) -> ElementTree.Element | None:
    return next((element for element in root if _local_name(element) == name), None)


def _child_text(root: ElementTree.Element, name: str) -> str | None:
    element = _child(root, name)
    return element.text if element is not None else None


def _safe_service_path(xaddr: str | None) -> str:
    if not xaddr:
        raise HomeCameraPTZError("ptz_service_unavailable")
    parsed = urlparse(xaddr)
    if parsed.scheme not in {"http", "https"} or not parsed.path.startswith("/onvif/"):
        raise HomeCameraPTZError("ptz_service_invalid")
    return parsed.path


def _ptz_ssh_argv(config: HomeCameraConfig, local_port: int) -> list[str]:
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
        f"127.0.0.1:{local_port}:{config.camera_ip}:{ONVIF_PORT}",
        "-N",
        config.ssh_alias,
    ]


@contextmanager
def temporary_onvif_forward(config: HomeCameraConfig) -> Iterator[int]:
    """Expose ONVIF only on a temporary server-local TCP listener."""
    local_port = _reserve_local_port()
    process = subprocess.Popen(
        _ptz_ssh_argv(config, local_port),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
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


@contextmanager
def _exclusive_ptz_lock(path: Path = DEFAULT_LOCK_PATH) -> Iterator[None]:
    flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW
    try:
        fd = os.open(path, flags, 0o600)
    except OSError as exc:
        raise HomeCameraPTZError("ptz_lock_unavailable") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0:
            raise HomeCameraPTZError("ptz_lock_invalid")
        os.fchmod(fd, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise HomeCameraPTZError("ptz_busy") from exc
        yield
    finally:
        os.close(fd)


class PTZController:
    """Authenticated ONVIF session with bounded movement primitives."""

    def __init__(self, config: HomeCameraConfig, local_port: int) -> None:
        self._config = config
        self._local_port = local_port
        self._profile: _PTZProfile | None = None

    def _envelope(self, body: str) -> bytes:
        nonce = os.urandom(20)
        created = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        password_digest = base64.b64encode(
            hashlib.sha1(nonce + created.encode() + self._config.password.encode()).digest()
        ).decode()
        nonce_b64 = base64.b64encode(nonce).decode()
        username = escape(self._config.username)
        return (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" '
            'xmlns:tds="http://www.onvif.org/ver10/device/wsdl" '
            'xmlns:trt="http://www.onvif.org/ver10/media/wsdl" '
            'xmlns:tptz="http://www.onvif.org/ver20/ptz/wsdl" '
            'xmlns:tt="http://www.onvif.org/ver10/schema" '
            'xmlns:wsse="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd" '
            'xmlns:wsu="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd">'
            '<s:Header><wsse:Security s:mustUnderstand="1"><wsse:UsernameToken>'
            f"<wsse:Username>{username}</wsse:Username>"
            '<wsse:Password Type="http://docs.oasis-open.org/wss/2004/01/'
            f'oasis-200401-wss-username-token-profile-1.0#PasswordDigest">{password_digest}</wsse:Password>'
            '<wsse:Nonce EncodingType="http://docs.oasis-open.org/wss/2004/01/'
            f'oasis-200401-wss-soap-message-security-1.0#Base64Binary">{nonce_b64}</wsse:Nonce>'
            f"<wsu:Created>{created}</wsu:Created>"
            "</wsse:UsernameToken></wsse:Security></s:Header>"
            f"<s:Body>{body}</s:Body></s:Envelope>"
        ).encode()

    def _call(self, path: str, body: str) -> ElementTree.Element:
        payload = self._envelope(body)
        connection = http.client.HTTPConnection("127.0.0.1", self._local_port, timeout=8)
        try:
            connection.request(
                "POST",
                path,
                payload,
                {
                    "Content-Type": "application/soap+xml; charset=utf-8",
                    "Connection": "close",
                },
            )
            response = connection.getresponse()
            content_length = response.getheader("Content-Length")
            if content_length is not None:
                try:
                    declared_length = int(content_length)
                except ValueError as exc:
                    raise HomeCameraPTZError("ptz_response_invalid") from exc
                if declared_length > _MAX_RESPONSE_BYTES:
                    raise HomeCameraPTZError("ptz_response_too_large")
            raw = response.read(_MAX_RESPONSE_BYTES + 1)
        except TimeoutError as exc:
            raise HomeCameraPTZError("ptz_timeout") from exc
        except OSError as exc:
            raise HomeCameraPTZError("ptz_connection_failed") from exc
        finally:
            connection.close()
        if response.status == 401:
            raise HomeCameraPTZError("ptz_authentication_failed")
        if response.status != 200:
            raise HomeCameraPTZError("ptz_request_failed")
        if len(raw) > _MAX_RESPONSE_BYTES:
            raise HomeCameraPTZError("ptz_response_too_large")
        try:
            root = ElementTree.fromstring(raw)
        except (ElementTree.ParseError, ValueError) as exc:
            raise HomeCameraPTZError("ptz_response_invalid") from exc
        if _first(root, "Fault") is not None:
            raise HomeCameraPTZError("ptz_request_failed")
        return root

    def _read_call(self, path: str, body: str) -> ElementTree.Element:
        """Retry one idempotent ONVIF read after a transient timeout."""
        for attempt in range(2):
            try:
                return self._call(path, body)
            except HomeCameraPTZError as exc:
                if attempt or str(exc) not in {"ptz_timeout", "ptz_connection_failed"}:
                    raise
                time.sleep(0.2)
        raise HomeCameraPTZError("ptz_timeout")  # pragma: no cover - loop always returns or raises.

    def _discover(self) -> _PTZProfile:
        if self._profile is not None:
            return self._profile
        capabilities = self._read_call(
            "/onvif/device_service",
            "<tds:GetCapabilities><tds:Category>All</tds:Category></tds:GetCapabilities>",
        )
        media = _first(capabilities, "Media")
        ptz = _first(capabilities, "PTZ")
        if media is None or ptz is None:
            raise HomeCameraPTZError("ptz_not_supported")
        media_path = _safe_service_path(_child_text(media, "XAddr"))
        ptz_path = _safe_service_path(_child_text(ptz, "XAddr"))
        profiles = self._read_call(media_path, "<trt:GetProfiles/>")
        profile = next(
            (
                candidate
                for candidate in profiles.iter()
                if _local_name(candidate) == "Profiles" and _first(candidate, "PTZConfiguration") is not None
            ),
            None,
        )
        if profile is None:
            raise HomeCameraPTZError("ptz_profile_missing")
        ptz_configuration = _first(profile, "PTZConfiguration")
        profile_token = profile.attrib.get("token")
        configuration_token = ptz_configuration.attrib.get("token") if ptz_configuration is not None else None
        if not profile_token or not configuration_token:
            raise HomeCameraPTZError("ptz_profile_missing")
        options = self._read_call(
            ptz_path,
            "<tptz:GetConfigurationOptions>"
            f"<tptz:ConfigurationToken>{escape(configuration_token)}</tptz:ConfigurationToken>"
            "</tptz:GetConfigurationOptions>",
        )
        self._profile = _PTZProfile(
            profile_token=profile_token,
            ptz_path=ptz_path,
            relative=self._parse_space(options, "RelativePanTiltTranslationSpace"),
            absolute=self._parse_space(options, "AbsolutePanTiltPositionSpace"),
            continuous=self._parse_space(options, "ContinuousPanTiltVelocitySpace"),
        )
        return self._profile

    @staticmethod
    def _parse_space(root: ElementTree.Element, name: str) -> _PTZSpace | None:
        node = _first(root, name)
        if node is None:
            return None
        x_range = _child(node, "XRange")
        y_range = _child(node, "YRange")
        uri = _child_text(node, "URI")
        if x_range is None or y_range is None or not uri:
            raise HomeCameraPTZError("ptz_options_invalid")
        try:
            space = _PTZSpace(
                uri=uri,
                x_min=float(_child_text(x_range, "Min") or ""),
                x_max=float(_child_text(x_range, "Max") or ""),
                y_min=float(_child_text(y_range, "Min") or ""),
                y_max=float(_child_text(y_range, "Max") or ""),
            )
            if not all(math.isfinite(value) for value in (space.x_min, space.x_max, space.y_min, space.y_max)):
                raise ValueError
            if space.x_min > space.x_max or space.y_min > space.y_max:
                raise ValueError
            return space
        except ValueError as exc:
            raise HomeCameraPTZError("ptz_options_invalid") from exc

    def status(self) -> PTZStatus:
        profile = self._discover()
        position = self._read_position(profile)
        return PTZStatus(
            captured_at=datetime.now(UTC).isoformat(),
            position=position,
            relative_supported=profile.relative is not None,
            absolute_supported=profile.absolute is not None,
            continuous_supported=profile.continuous is not None,
        )

    def _read_position(self, profile: _PTZProfile | None = None) -> PTZPosition:
        selected = profile or self._discover()
        root = self._read_call(
            selected.ptz_path,
            f"<tptz:GetStatus><tptz:ProfileToken>{escape(selected.profile_token)}</tptz:ProfileToken></tptz:GetStatus>",
        )
        parent = _first(root, "Position")
        pan_tilt = _first(parent, "PanTilt") if parent is not None else None
        if pan_tilt is None:
            raise HomeCameraPTZError("ptz_position_unavailable")
        try:
            position = PTZPosition(float(pan_tilt.attrib["x"]), float(pan_tilt.attrib["y"]))
            if not math.isfinite(position.pan) or not math.isfinite(position.tilt):
                raise ValueError
            return position
        except (KeyError, ValueError) as exc:
            raise HomeCameraPTZError("ptz_position_invalid") from exc

    def move_relative(
        self,
        direction: Literal["left", "right", "up", "down"],
        step: Literal["small", "medium"],
    ) -> PTZMoveResult:
        if direction not in _DIRECTION_VECTORS:
            raise ValueError("direction must be left, right, up, or down")
        if step not in _STEP_VALUES:
            raise ValueError("step must be small or medium")
        profile = self._discover()
        if profile.relative is None:
            raise HomeCameraPTZError("ptz_relative_not_supported")
        before = self._read_position(profile)
        multiplier = _STEP_VALUES[step]
        unit_x, unit_y = _DIRECTION_VECTORS[direction]
        delta_x, delta_y = unit_x * multiplier, unit_y * multiplier
        self._validate_relative_target(before, delta_x, delta_y, profile)
        space = quoteattr(profile.relative.uri)
        body = (
            "<tptz:RelativeMove>"
            f"<tptz:ProfileToken>{escape(profile.profile_token)}</tptz:ProfileToken>"
            f"<tptz:Translation><tt:PanTilt x={quoteattr(str(delta_x))} y={quoteattr(str(delta_y))} "
            f"space={space}/></tptz:Translation>"
            "</tptz:RelativeMove>"
        )
        try:
            self._call(profile.ptz_path, body)
            after = self._wait_for_position(before, profile)
        except BaseException:
            self._best_effort_stop()
            raise
        return PTZMoveResult(direction, step, before, after, datetime.now(UTC).isoformat())

    @staticmethod
    def _validate_relative_target(
        before: PTZPosition,
        delta_x: float,
        delta_y: float,
        profile: _PTZProfile,
    ) -> None:
        relative = profile.relative
        if relative is None or not (
            relative.x_min <= delta_x <= relative.x_max and relative.y_min <= delta_y <= relative.y_max
        ):
            raise HomeCameraPTZError("ptz_step_out_of_range")
        absolute = profile.absolute
        if absolute is not None and not (
            absolute.x_min <= before.pan + delta_x <= absolute.x_max
            and absolute.y_min <= before.tilt + delta_y <= absolute.y_max
        ):
            raise HomeCameraPTZError("ptz_target_out_of_range")

    def goto(self, position: PTZPosition) -> PTZPosition:
        """Restore a previously read position; intended for bounded scan cleanup."""
        profile = self._discover()
        if profile.absolute is None:
            raise HomeCameraPTZError("ptz_absolute_not_supported")
        absolute = profile.absolute
        if not (absolute.x_min <= position.pan <= absolute.x_max and absolute.y_min <= position.tilt <= absolute.y_max):
            raise HomeCameraPTZError("ptz_target_out_of_range")
        before = self._read_position(profile)
        body = (
            "<tptz:AbsoluteMove>"
            f"<tptz:ProfileToken>{escape(profile.profile_token)}</tptz:ProfileToken>"
            f"<tptz:Position><tt:PanTilt x={quoteattr(str(position.pan))} y={quoteattr(str(position.tilt))} "
            f"space={quoteattr(absolute.uri)}/></tptz:Position>"
            "</tptz:AbsoluteMove>"
        )
        try:
            self._call(profile.ptz_path, body)
            return self._wait_for_position(before, profile, target=position)
        except BaseException:
            self._best_effort_stop()
            raise

    def _wait_for_position(
        self,
        before: PTZPosition,
        profile: _PTZProfile,
        *,
        target: PTZPosition | None = None,
    ) -> PTZPosition:
        deadline = time.monotonic() + _MOVE_TIMEOUT_SECONDS
        previous: PTZPosition | None = None
        changed = False
        stable_count = 0
        while time.monotonic() < deadline:
            time.sleep(0.2)
            current = self._read_position(profile)
            if _distance(current, before) > _POSITION_TOLERANCE:
                changed = True
            if target is not None and _distance(current, target) <= _POSITION_TOLERANCE:
                return current
            if previous is not None and _distance(current, previous) <= _POSITION_TOLERANCE:
                stable_count += 1
            else:
                stable_count = 0
            if changed and stable_count >= 2:
                return current
            previous = current
        raise HomeCameraPTZError("ptz_move_timeout")

    def stop(self) -> None:
        profile = self._discover()
        self._call(
            profile.ptz_path,
            "<tptz:Stop>"
            f"<tptz:ProfileToken>{escape(profile.profile_token)}</tptz:ProfileToken>"
            "<tptz:PanTilt>true</tptz:PanTilt><tptz:Zoom>true</tptz:Zoom>"
            "</tptz:Stop>",
        )

    def _best_effort_stop(self) -> None:
        with suppress(HomeCameraError):
            self.stop()


def _distance(left: PTZPosition, right: PTZPosition) -> float:
    return max(abs(left.pan - right.pan), abs(left.tilt - right.tilt))


@contextmanager
def open_ptz_controller(
    config_path: Path = DEFAULT_CONFIG_PATH,
    *,
    lock_path: Path = DEFAULT_LOCK_PATH,
) -> Iterator[PTZController]:
    """Open one exclusive, temporary ONVIF control session."""
    config = load_config(config_path)
    with _exclusive_ptz_lock(lock_path), temporary_onvif_forward(config) as local_port:
        yield PTZController(config, local_port)


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Bounded owner-authorized PTZ control for the private home camera.")
    parser.add_argument("--action", choices=("status", "move", "goto", "stop"), required=True)
    parser.add_argument("--direction", choices=tuple(_DIRECTION_VECTORS))
    parser.add_argument("--step", choices=tuple(_STEP_VALUES), default="small")
    parser.add_argument("--pan", type=float, help="Previously read pan coordinate for bounded restore.")
    parser.add_argument("--tilt", type=float, help="Previously read tilt coordinate for bounded restore.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.action == "move" and args.direction is None:
        parser.error("--direction is required for --action move")
    if args.action == "goto" and (args.pan is None or args.tilt is None):
        parser.error("--pan and --tilt are required for --action goto")
    try:
        with open_ptz_controller(args.config) as controller:
            if args.action == "status":
                result = asdict(controller.status())
            elif args.action == "stop":
                controller.stop()
                result = {"action": "stop"}
            elif args.action == "goto":
                result = {"action": "goto", "position": asdict(controller.goto(PTZPosition(args.pan, args.tilt)))}
            else:
                result = asdict(controller.move_relative(args.direction, args.step))
    except HomeCameraError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 2
    print(json.dumps({"ok": True, **result}, ensure_ascii=False))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
