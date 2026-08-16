"""Bounded one-shot capture from an allowlisted Krasnoyarsk public camera."""

from __future__ import annotations

from contextlib import suppress
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import pwd
import re
import secrets
import stat
import subprocess
from typing import Any
import unicodedata
from urllib.parse import urlparse

CAMERA_ID = "c_6171"
CAMERA_TITLE = "Семафорная 185"
CAMERA_PAGE_URL = "https://24oko.ru/city"
CAMERA_PLAYER_PATH = f"/request/camera/url/{CAMERA_ID}"
_CAMERA_REGISTRY_PATH = Path(__file__).resolve().parents[1] / "docs/agent/public_camera_registry.json"
_PLAYER_HOST = re.compile(r"^fl-[0-9]+\.telecoma\.tv$", re.IGNORECASE)
_CAMERA_KEY = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_PROVIDER_CAMERA_ID = re.compile(r"^c_[0-9]+$")
_RUNNER_USER = "autostop-public-camera"
_RUNNER_RUNTIME_DIRECTORY = Path("/run/autostop-public-camera")
_RUNNER_SITE_PACKAGES = Path("/opt/autostop-public-camera-runtime/site-packages")
_RUNNER_PYTHON = "/usr/bin/python3"
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_MAX_RUNNER_OUTPUT_BYTES = 10 * 1024 * 1024
_QUERY_STOPWORDS = {
    "актуальный",
    "возле",
    "городская",
    "дай",
    "кадр",
    "камера",
    "камеры",
    "красноярск",
    "красноярске",
    "на",
    "наружная",
    "общественная",
    "около",
    "открытая",
    "по",
    "покажи",
    "пришли",
    "публичная",
    "с",
    "свежий",
    "сейчас",
    "снимок",
    "со",
    "текущий",
    "у",
    "улица",
    "фото",
    "фотография",
}


class PublicCameraError(RuntimeError):
    """A safe failure from the public-camera controller or worker."""


@dataclass(frozen=True)
class PublicCamera:
    key: str
    provider_camera_id: str
    title: str
    aliases: tuple[str, ...]
    latitude: float
    longitude: float
    status: str
    last_verified_at: str | None

    def public_summary(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "title": self.title,
            "aliases": list(self.aliases),
            "latitude": self.latitude,
            "longitude": self.longitude,
            "status": self.status,
            "last_verified_at": self.last_verified_at,
        }


@dataclass(frozen=True)
class CameraCapture:
    camera_key: str
    camera_id: str
    title: str
    captured_at: str
    screenshot: str


@dataclass(frozen=True)
class _RunnerAccount:
    uid: int
    gid: int


def _home_camera_module():
    """Load the private-camera output helper only in the root controller."""
    from autostop_manager import home_camera

    return home_camera


def _parse_camera_record(raw: Any) -> PublicCamera:
    if not isinstance(raw, dict):
        raise PublicCameraError("public_camera_registry_invalid")
    try:
        key = raw["key"]
        camera_id = raw["provider_camera_id"]
        title = raw["title"]
        aliases = raw["aliases"]
        latitude = raw["latitude"]
        longitude = raw["longitude"]
        status = raw["status"]
        last_verified_at = raw["last_verified_at"]
    except KeyError as exc:
        raise PublicCameraError("public_camera_registry_invalid") from exc
    if not isinstance(key, str) or _CAMERA_KEY.fullmatch(key) is None:
        raise PublicCameraError("public_camera_registry_invalid")
    if not isinstance(camera_id, str) or _PROVIDER_CAMERA_ID.fullmatch(camera_id) is None:
        raise PublicCameraError("public_camera_registry_invalid")
    if not isinstance(title, str) or not 1 <= len(title) <= 120:
        raise PublicCameraError("public_camera_registry_invalid")
    if (
        not isinstance(aliases, list)
        or not 1 <= len(aliases) <= 20
        or any(not isinstance(alias, str) or not 1 <= len(alias) <= 120 for alias in aliases)
    ):
        raise PublicCameraError("public_camera_registry_invalid")
    if not isinstance(latitude, (int, float)) or not 55.8 <= float(latitude) <= 56.3:
        raise PublicCameraError("public_camera_registry_invalid")
    if not isinstance(longitude, (int, float)) or not 92.5 <= float(longitude) <= 93.3:
        raise PublicCameraError("public_camera_registry_invalid")
    if status not in {"endpoint_verified", "working", "unavailable"}:
        raise PublicCameraError("public_camera_registry_invalid")
    if last_verified_at is not None and not isinstance(last_verified_at, str):
        raise PublicCameraError("public_camera_registry_invalid")
    return PublicCamera(
        key=key,
        provider_camera_id=camera_id,
        title=title,
        aliases=tuple(aliases),
        latitude=float(latitude),
        longitude=float(longitude),
        status=status,
        last_verified_at=last_verified_at,
    )


def load_public_camera_registry() -> tuple[PublicCamera, ...]:
    """Load and validate the root-owned public camera allowlist."""
    try:
        payload = json.loads(_CAMERA_REGISTRY_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise PublicCameraError("public_camera_registry_invalid") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("format") != "autostop_public_camera_registry_v1"
        or payload.get("provider") != {"name": "24oko", "public_page": CAMERA_PAGE_URL}
        or not isinstance(payload.get("cameras"), list)
    ):
        raise PublicCameraError("public_camera_registry_invalid")
    cameras = tuple(_parse_camera_record(raw) for raw in payload["cameras"])
    if not cameras or len({camera.key for camera in cameras}) != len(cameras):
        raise PublicCameraError("public_camera_registry_invalid")
    if len({camera.provider_camera_id for camera in cameras}) != len(cameras):
        raise PublicCameraError("public_camera_registry_invalid")
    return cameras


def get_public_camera(camera_key: str) -> PublicCamera:
    """Return exactly one allowlisted camera by its stable key."""
    for camera in load_public_camera_registry():
        if camera.key == camera_key:
            return camera
    raise PublicCameraError("public_camera_not_allowlisted")


def _normalize_camera_query(value: str) -> tuple[str, ...]:
    value = unicodedata.normalize("NFKC", value).casefold().replace("ё", "е")
    value = re.sub(r"\b9\s*[- ]?го\b", "9", value)
    return tuple(token for token in re.findall(r"[0-9a-zа-я]+", value) if token not in _QUERY_STOPWORDS)


def search_public_cameras(query: str) -> tuple[PublicCamera, ...]:
    """Resolve a street, address, or landmark without guessing between ties."""
    query_tokens = _normalize_camera_query(query)
    if not query_tokens:
        return ()
    cameras = load_public_camera_registry()
    exact: list[PublicCamera] = []
    query_key = " ".join(query_tokens)
    for camera in cameras:
        phrases = (camera.title, camera.key, *camera.aliases)
        if any(" ".join(_normalize_camera_query(phrase)) == query_key for phrase in phrases):
            exact.append(camera)
    if exact:
        return tuple(exact)

    query_set = set(query_tokens)
    scored: list[tuple[int, PublicCamera]] = []
    for camera in cameras:
        best = 0
        for phrase in (camera.title, *camera.aliases):
            phrase_set = set(_normalize_camera_query(phrase))
            overlap = len(query_set & phrase_set)
            if overlap and (query_set <= phrase_set or phrase_set <= query_set):
                best = max(best, overlap)
        if best:
            scored.append((best, camera))
    if not scored:
        return ()
    top_score = max(score for score, _camera in scored)
    return tuple(camera for score, camera in scored if score == top_score)


def resolve_public_camera(query: str) -> PublicCamera:
    """Resolve one camera or return a safe not-found/ambiguous error."""
    matches = search_public_cameras(query)
    if not matches:
        raise PublicCameraError("public_camera_not_found")
    if len(matches) != 1:
        raise PublicCameraError("public_camera_query_ambiguous")
    return matches[0]


def extract_public_player_url(
    payload: dict[str, Any],
    camera: PublicCamera | None = None,
) -> str:
    """Validate the provider payload before embedding its current player URL."""
    if camera is None:
        camera = get_public_camera("semafornaya-185")
    if payload.get("overlayTitle") != camera.title:
        raise PublicCameraError("unexpected_camera_title")
    content = payload.get("content")
    if not isinstance(content, str):
        raise PublicCameraError("camera_player_missing")

    match = re.search(r"<iframe\b[^>]*\bsrc=[\"']([^\"']+)[\"']", content, re.IGNORECASE)
    if match is None:
        raise PublicCameraError("camera_iframe_missing")
    player_url = match.group(1)
    parsed = urlparse(player_url)
    if (
        parsed.scheme != "https"
        or _PLAYER_HOST.fullmatch(parsed.hostname or "") is None
        or parsed.port not in {None, 443}
        or not parsed.path.endswith("/embed.mp4")
    ):
        raise PublicCameraError("unexpected_camera_player")
    return player_url


def _require_root_controller() -> None:
    if os.geteuid() != 0:
        raise PublicCameraError("public_camera_controller_must_be_root")


def _runner_account() -> _RunnerAccount:
    try:
        entry = pwd.getpwnam(_RUNNER_USER)
    except KeyError as exc:
        raise PublicCameraError("public_camera_runner_account_missing") from exc
    if entry.pw_uid == 0:
        raise PublicCameraError("public_camera_runner_account_invalid")
    return _RunnerAccount(entry.pw_uid, entry.pw_gid)


def _ensure_runner_runtime_directory() -> None:
    _RUNNER_RUNTIME_DIRECTORY.mkdir(mode=0o711, exist_ok=True)
    info = _RUNNER_RUNTIME_DIRECTORY.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0:
        raise PublicCameraError("public_camera_runner_runtime_invalid")
    if stat.S_IMODE(info.st_mode) != 0o711:
        raise PublicCameraError("public_camera_runner_runtime_permissions_invalid")


def _create_runner_output(account: _RunnerAccount) -> Path:
    _ensure_runner_runtime_directory()
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW
    for _ in range(100):
        path = _RUNNER_RUNTIME_DIRECTORY / f"job-{secrets.token_hex(16)}.png"
        try:
            file_fd = os.open(path, flags, 0o600)
        except FileExistsError:
            continue
        try:
            os.fchown(file_fd, account.uid, account.gid)
            os.fchmod(file_fd, 0o600)
        finally:
            os.close(file_fd)
        return path
    raise PublicCameraError("public_camera_runner_output_unavailable")


def _runner_argv(
    account: _RunnerAccount,
    *,
    camera_key: str | None = None,
    output_path: Path | None = None,
    wait_ms: int = 8_000,
    self_test: bool = False,
    private_network: bool = False,
) -> list[str]:
    unit_name = f"autostop-public-camera-{secrets.token_hex(8)}"
    argv = [
        "systemd-run",
        "--quiet",
        "--wait",
        "--pipe",
        "--collect",
        f"--unit={unit_name}",
        f"--property=User={_RUNNER_USER}",
        f"--property=Group={account.gid}",
        "--property=UMask=0077",
        "--property=WorkingDirectory=/",
        "--property=NoNewPrivileges=yes",
        "--property=RestrictSUIDSGID=yes",
        "--property=CapabilityBoundingSet=",
        "--property=PrivateTmp=yes",
        "--property=PrivateDevices=yes",
        "--property=PrivateIPC=yes",
        "--property=ProtectSystem=strict",
        "--property=ProtectHome=tmpfs",
        "--property=ProtectControlGroups=yes",
        "--property=ProtectKernelModules=yes",
        "--property=ProtectKernelTunables=yes",
        "--property=ProtectKernelLogs=yes",
        "--property=ProtectClock=yes",
        "--property=ProtectHostname=yes",
        "--property=ProtectProc=invisible",
        "--property=ProcSubset=pid",
        "--property=RestrictNamespaces=yes",
        "--property=RestrictRealtime=yes",
        "--property=LockPersonality=yes",
        "--property=SystemCallArchitectures=native",
        "--property=RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6",
        f"--property=ReadWritePaths={_RUNNER_RUNTIME_DIRECTORY}",
        "--property=TimeoutStartSec=45s",
        "--property=TimeoutStopSec=10s",
        "--property=KillMode=control-group",
        f"--setenv=PYTHONPATH={_RUNNER_SITE_PACKAGES}:{_PROJECT_ROOT}",
        "--setenv=PYTHONDONTWRITEBYTECODE=1",
        "--setenv=HOME=/tmp",
        "--setenv=XDG_CACHE_HOME=/tmp/autostop-public-camera-cache",
    ]
    if private_network:
        argv.append("--property=PrivateNetwork=yes")
    argv.extend(["--", _RUNNER_PYTHON, "-m", "autostop_manager.public_camera_worker"])
    if self_test:
        argv.append("--self-test")
    else:
        if output_path is None or camera_key is None:
            raise ValueError("camera_key and output_path are required for a capture run")
        argv.extend(
            [
                "--camera-key",
                camera_key,
                "--output",
                str(output_path),
                "--wait-ms",
                str(wait_ms),
            ]
        )
    return argv


def _run_worker(argv: list[str]) -> None:
    try:
        result = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PublicCameraError("public_camera_runner_unavailable") from exc
    if result.returncode != 0:
        raise PublicCameraError("public_camera_runner_failed")
    try:
        payload = json.loads(result.stdout)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise PublicCameraError("public_camera_runner_failed") from exc
    if payload != {"ok": True}:
        raise PublicCameraError("public_camera_runner_failed")


def _copy_verified_runner_output(
    source_path: Path,
    account: _RunnerAccount,
    reservation: Any,
) -> None:
    try:
        source_fd = os.open(source_path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError as exc:
        raise PublicCameraError("public_camera_runner_output_invalid") from exc
    try:
        info = os.fstat(source_fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != account.uid:
            raise PublicCameraError("public_camera_runner_output_invalid")
        if stat.S_IMODE(info.st_mode) != 0o600 or not 24 <= info.st_size <= _MAX_RUNNER_OUTPUT_BYTES:
            raise PublicCameraError("public_camera_runner_output_invalid")
        header = os.read(source_fd, 24)
        if header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR":
            raise PublicCameraError("public_camera_runner_output_invalid")
        width = int.from_bytes(header[16:20], "big")
        height = int.from_bytes(header[20:24], "big")
        if not 0 < width <= 1280 or not 0 < height <= 720:
            raise PublicCameraError("public_camera_runner_output_invalid")
        os.lseek(source_fd, 0, os.SEEK_SET)
        destination_fd = os.open(
            reservation.staging_name,
            os.O_WRONLY | os.O_TRUNC | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=reservation.directory_fd,
        )
        try:
            while chunk := os.read(source_fd, 1024 * 1024):
                view = memoryview(chunk)
                while view:
                    view = view[os.write(destination_fd, view) :]
            os.fchmod(destination_fd, 0o600)
        finally:
            os.close(destination_fd)
    finally:
        os.close(source_fd)


def _reserve_public_output(path: Path, *, overwrite: bool) -> Any:
    home_camera = _home_camera_module()
    try:
        return home_camera._reserve_output(path, overwrite=overwrite)
    except home_camera.HomeCameraError as exc:
        raise PublicCameraError(str(exc)) from exc


def verify_public_camera_runner() -> None:
    """Verify the unprivileged runner using only ``about:blank`` and no network."""
    _require_root_controller()
    account = _runner_account()
    _ensure_runner_runtime_directory()
    _run_worker(_runner_argv(account, self_test=True, private_network=True))


def capture_public_camera(
    camera_key: str,
    output_path: Path,
    *,
    overwrite: bool = False,
    wait_ms: int = 8_000,
    browser_path: str | None = None,
) -> CameraCapture:
    """Save one allowlisted public frame through the non-root browser runner."""
    if not 0 <= wait_ms <= 15_000:
        raise ValueError("wait_ms must be between 0 and 15000")
    if browser_path is not None:
        raise PublicCameraError("browser_path_not_supported")
    camera = get_public_camera(camera_key)
    _require_root_controller()
    account = _runner_account()
    reservation = _reserve_public_output(output_path, overwrite=overwrite)
    home_camera = _home_camera_module()
    runner_output: Path | None = None
    published = False
    try:
        runner_output = _create_runner_output(account)
        _run_worker(
            _runner_argv(
                account,
                camera_key=camera.key,
                output_path=runner_output,
                wait_ms=wait_ms,
            )
        )
        _copy_verified_runner_output(runner_output, account, reservation)
        home_camera._publish_output(reservation)
        published = True
    finally:
        if runner_output is not None:
            with suppress(FileNotFoundError):
                runner_output.unlink()
        try:
            if not published:
                home_camera._discard_staging_output(reservation)
        finally:
            os.close(reservation.directory_fd)

    return CameraCapture(
        camera_key=camera.key,
        camera_id=camera.provider_camera_id,
        title=camera.title,
        captured_at=datetime.now(UTC).isoformat(),
        screenshot=str(output_path),
    )


def capture_semafornaya_185(
    output_path: Path,
    *,
    overwrite: bool = False,
    wait_ms: int = 8_000,
    browser_path: str | None = None,
) -> CameraCapture:
    """Compatibility entrypoint for the AutoStop Semafornaya 185 camera."""
    return capture_public_camera(
        "semafornaya-185",
        output_path,
        overwrite=overwrite,
        wait_ms=wait_ms,
        browser_path=browser_path,
    )


def main(argv: list[str] | None = None, *, default_camera_key: str | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="List, resolve, or capture one allowlisted public camera.")
    parser.add_argument("--list", action="store_true", help="List allowlisted cameras without capturing.")
    parser.add_argument("--verify-runner", action="store_true", help="Verify the sandboxed browser without network.")
    parser.add_argument("--resolve", help="Resolve a street, address, or landmark without capturing.")
    parser.add_argument("--camera", help="Exact allowlisted camera key.")
    parser.add_argument("--query", help="Resolve one camera from a street, address, or landmark and capture it.")
    parser.add_argument("--output", type=Path, help="Exact output PNG path; parent directory must exist.")
    parser.add_argument("--overwrite", action="store_true", help="Allow replacing the exact output file.")
    parser.add_argument("--wait-ms", type=int, default=8_000, help="Single render wait, from 0 to 15000 ms.")
    parser.add_argument("--browser-path", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    try:
        if args.verify_runner:
            if args.list or args.resolve or args.query or args.camera or args.output:
                parser.error("--verify-runner cannot be combined with list, resolve, or capture arguments")
            verify_public_camera_runner()
            print(json.dumps({"ok": True, "runner": "verified"}))
            return 0
        if args.list:
            if args.resolve or args.query or args.camera or args.output:
                parser.error("--list cannot be combined with capture or resolve arguments")
            payload = {
                "format": "autostop_public_camera_list_v1",
                "cameras": [camera.public_summary() for camera in load_public_camera_registry()],
            }
            print(json.dumps(payload, ensure_ascii=False))
            return 0
        if args.resolve:
            if args.query or args.camera or args.output:
                parser.error("--resolve cannot be combined with capture arguments")
            matches = search_public_cameras(args.resolve)
            print(
                json.dumps(
                    {
                        "format": "autostop_public_camera_resolution_v1",
                        "match_count": len(matches),
                        "matches": [camera.public_summary() for camera in matches],
                    },
                    ensure_ascii=False,
                )
            )
            return 0

        camera_key = args.camera or default_camera_key
        if args.query:
            if args.camera or default_camera_key:
                parser.error("--query cannot be combined with an exact camera")
            camera_key = resolve_public_camera(args.query).key
        if camera_key is None or args.output is None:
            parser.error("capture requires --camera or --query and --output")
        result = capture_public_camera(
            camera_key,
            args.output,
            overwrite=args.overwrite,
            wait_ms=args.wait_ms,
            browser_path=args.browser_path,
        )
    except PublicCameraError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 2
    print(json.dumps(asdict(result), ensure_ascii=False))
    return 0


if __name__ == "__main__":  # pragma: no cover - standalone operational helper.
    raise SystemExit(main())
