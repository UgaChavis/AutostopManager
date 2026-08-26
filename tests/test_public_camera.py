from __future__ import annotations

import json
import os
from pathlib import Path
import stat
from types import SimpleNamespace

import pytest

from autostop_manager import home_camera, public_camera, public_camera_worker
from autostop_manager.public_camera import CAMERA_TITLE, PublicCameraError, extract_public_player_url


def _camera_record(**overrides):
    record = {
        "key": "test-camera",
        "provider_camera_id": "c_123",
        "title": "Test Camera",
        "aliases": ["Test landmark"],
        "latitude": 56.0,
        "longitude": 92.9,
        "status": "working",
        "last_verified_at": "2026-08-20T00:00:00Z",
    }
    record.update(overrides)
    return record


@pytest.fixture
def root_camera_controller(monkeypatch):
    """Model the root controller while preserving runner-file ownership checks."""
    real_fstat = home_camera.os.fstat
    real_stat = home_camera.os.stat

    def root_owned(info):
        return SimpleNamespace(st_mode=info.st_mode, st_uid=0)

    def root_owned_directories(fd):
        info = real_fstat(fd)
        return root_owned(info) if stat.S_ISDIR(info.st_mode) else info

    def root_owned_dir_entry(path, *args, **kwargs):
        info = real_stat(path, *args, **kwargs)
        if kwargs.get("dir_fd") is not None:
            return root_owned(info)
        return info

    monkeypatch.setattr(public_camera.os, "geteuid", lambda: 0)
    monkeypatch.setattr(home_camera.os, "fstat", root_owned_directories)
    monkeypatch.setattr(home_camera.os, "stat", root_owned_dir_entry)


def test_extract_public_player_url_accepts_expected_public_player():
    payload = {
        "overlayTitle": CAMERA_TITLE,
        "content": '<iframe src="https://fl-4.telecoma.tv:443/semd185_1/embed.mp4?"></iframe>',
    }

    assert extract_public_player_url(payload) == "https://fl-4.telecoma.tv:443/semd185_1/embed.mp4?"


@pytest.mark.parametrize(
    "payload, reason",
    [
        ({"overlayTitle": "Другая камера", "content": ""}, "unexpected_camera_title"),
        ({"overlayTitle": CAMERA_TITLE, "content": None}, "camera_player_missing"),
        ({"overlayTitle": CAMERA_TITLE, "content": "<div>no player</div>"}, "camera_iframe_missing"),
        (
            {"overlayTitle": CAMERA_TITLE, "content": '<iframe src="https://example.com/embed.mp4"></iframe>'},
            "unexpected_camera_player",
        ),
    ],
)
def test_extract_public_player_url_rejects_unexpected_payload(payload, reason):
    with pytest.raises(PublicCameraError, match=reason):
        extract_public_player_url(payload)


def test_public_controller_rejects_custom_browser_path(tmp_path):
    with pytest.raises(PublicCameraError, match="browser_path_not_supported"):
        public_camera.capture_public_camera(
            "semafornaya-185",
            tmp_path / "frame.png",
            browser_path="/tmp/browser",
        )


def test_registry_contains_existing_camera_plus_ten_new_allowlisted_points():
    cameras = public_camera.load_public_camera_registry()

    assert len(cameras) == 11
    assert cameras[0].key == "semafornaya-185"
    assert len({camera.key for camera in cameras}) == 11
    assert len({camera.provider_camera_id for camera in cameras}) == 11
    assert {camera.status for camera in cameras} == {"working"}
    assert all(camera.last_verified_at for camera in cameras)


@pytest.mark.parametrize(
    "field,value",
    [
        ("key", "Bad Key"),
        ("provider_camera_id", "camera-1"),
        ("title", ""),
        ("aliases", []),
        ("latitude", 1.0),
        ("longitude", 1.0),
        ("status", "unknown"),
        ("last_verified_at", 123),
    ],
)
def test_registry_record_rejects_each_invalid_security_field(field, value):
    with pytest.raises(PublicCameraError, match="public_camera_registry_invalid"):
        public_camera._parse_camera_record(_camera_record(**{field: value}))


def test_registry_record_rejects_wrong_shape_and_missing_fields():
    with pytest.raises(PublicCameraError, match="public_camera_registry_invalid"):
        public_camera._parse_camera_record([])
    with pytest.raises(PublicCameraError, match="public_camera_registry_invalid"):
        public_camera._parse_camera_record({})


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {
            "format": "autostop_public_camera_registry_v1",
            "provider": {"name": "24oko", "public_page": public_camera.CAMERA_PAGE_URL},
            "cameras": [],
        },
        {
            "format": "autostop_public_camera_registry_v1",
            "provider": {"name": "24oko", "public_page": public_camera.CAMERA_PAGE_URL},
            "cameras": [_camera_record(), _camera_record(provider_camera_id="c_124")],
        },
        {
            "format": "autostop_public_camera_registry_v1",
            "provider": {"name": "24oko", "public_page": public_camera.CAMERA_PAGE_URL},
            "cameras": [_camera_record(), _camera_record(key="other-camera")],
        },
    ],
)
def test_registry_loader_rejects_invalid_or_duplicate_allowlists(monkeypatch, tmp_path, payload):
    registry = tmp_path / "registry.json"
    registry.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(public_camera, "_CAMERA_REGISTRY_PATH", registry)

    with pytest.raises(PublicCameraError, match="public_camera_registry_invalid"):
        public_camera.load_public_camera_registry()


def test_registry_loader_rejects_unreadable_json(monkeypatch, tmp_path):
    registry = tmp_path / "registry.json"
    registry.write_text("not-json", encoding="utf-8")
    monkeypatch.setattr(public_camera, "_CAMERA_REGISTRY_PATH", registry)

    with pytest.raises(PublicCameraError, match="public_camera_registry_invalid"):
        public_camera.load_public_camera_registry()


@pytest.mark.parametrize(
    "query, expected_key",
    [
        ("пришли фото с камеры у БКЗ", "karla-marksa-bkz"),
        ("покажи 9-го Мая", "9-maya-78-dobrovolcheskoy"),
        ("снимок с Предмостной площади", "predmostnaya-ploshchad"),
        ("что сейчас у автосервиса AutoStop", "semafornaya-185"),
    ],
)
def test_resolve_public_camera_by_landmark_or_street(query, expected_key):
    assert public_camera.resolve_public_camera(query).key == expected_key


def test_resolve_public_camera_does_not_guess_between_matches():
    with pytest.raises(PublicCameraError, match="public_camera_query_ambiguous"):
        public_camera.resolve_public_camera("Молокова")


def test_public_camera_search_returns_empty_for_empty_and_unknown_queries():
    assert public_camera.search_public_cameras("покажи текущий снимок с камеры") == ()
    assert public_camera.search_public_cameras("несуществующий ориентир") == ()
    with pytest.raises(PublicCameraError, match="public_camera_not_found"):
        public_camera.resolve_public_camera("несуществующий ориентир")


def test_public_camera_rejects_non_allowlisted_key(tmp_path):
    with pytest.raises(PublicCameraError, match="public_camera_not_allowlisted"):
        public_camera.capture_public_camera("c-999999", tmp_path / "frame.png")


def test_runner_argv_uses_nonroot_systemd_sandbox(monkeypatch):
    monkeypatch.setattr(public_camera.secrets, "token_hex", lambda _size: "fixed")
    account = public_camera._RunnerAccount(uid=990, gid=984)

    argv = public_camera._runner_argv(
        account,
        camera_key="semafornaya-185",
        output_path=Path("/run/autostop-public-camera/job-fixed.png"),
        wait_ms=1_000,
    )

    assert "--property=User=autostop-public-camera" in argv
    assert "--property=Group=984" in argv
    assert "--property=NoNewPrivileges=yes" in argv
    assert "--property=PrivateTmp=yes" in argv
    assert "--property=ProtectSystem=strict" in argv
    assert "--property=ProtectHome=tmpfs" in argv
    assert "--property=RestrictNamespaces=yes" in argv
    assert "--property=ReadWritePaths=/run/autostop-public-camera" in argv
    assert "--property=TimeoutStartSec=45s" in argv
    assert f"--setenv=PYTHONPATH={public_camera._RUNNER_SITE_PACKAGES}:{public_camera._PROJECT_ROOT}" in argv
    assert "--no-sandbox" not in argv
    assert argv[-6:] == [
        "--camera-key",
        "semafornaya-185",
        "--output",
        "/run/autostop-public-camera/job-fixed.png",
        "--wait-ms",
        "1000",
    ]


def test_runner_argv_self_test_has_private_network_and_requires_capture_arguments():
    account = public_camera._RunnerAccount(uid=990, gid=984)
    argv = public_camera._runner_argv(account, self_test=True, private_network=True)

    assert "--property=PrivateNetwork=yes" in argv
    assert argv[-1] == "--self-test"
    with pytest.raises(ValueError, match="camera_key and output_path"):
        public_camera._runner_argv(account)


def test_public_controller_requires_root_and_valid_runner_account(monkeypatch):
    monkeypatch.setattr(public_camera.os, "geteuid", lambda: 1000)
    with pytest.raises(PublicCameraError, match="controller_must_be_root"):
        public_camera._require_root_controller()

    monkeypatch.setattr(
        public_camera.pwd,
        "getpwnam",
        lambda _name: (_ for _ in ()).throw(KeyError("missing")),
    )
    with pytest.raises(PublicCameraError, match="runner_account_missing"):
        public_camera._runner_account()

    monkeypatch.setattr(public_camera.pwd, "getpwnam", lambda _name: SimpleNamespace(pw_uid=0, pw_gid=0))
    with pytest.raises(PublicCameraError, match="runner_account_invalid"):
        public_camera._runner_account()

    monkeypatch.setattr(public_camera.pwd, "getpwnam", lambda _name: SimpleNamespace(pw_uid=990, pw_gid=984))
    assert public_camera._runner_account() == public_camera._RunnerAccount(990, 984)


@pytest.mark.parametrize(
    "mode,uid,error",
    [
        (stat.S_IFREG | 0o711, 0, "runtime_invalid"),
        (stat.S_IFDIR | 0o711, 1000, "runtime_invalid"),
        (stat.S_IFDIR | 0o700, 0, "runtime_permissions_invalid"),
    ],
)
def test_runner_runtime_directory_contract(monkeypatch, mode, uid, error):
    runtime = SimpleNamespace(
        mkdir=lambda **_kwargs: None,
        lstat=lambda: SimpleNamespace(st_mode=mode, st_uid=uid),
    )
    monkeypatch.setattr(public_camera, "_RUNNER_RUNTIME_DIRECTORY", runtime)

    with pytest.raises(PublicCameraError, match=error):
        public_camera._ensure_runner_runtime_directory()


def test_runner_output_creation_handles_collision_and_sets_owner(monkeypatch):
    calls: list[tuple] = []
    attempts = 0

    def opened(*args):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise FileExistsError
        calls.append(args)
        return 41

    monkeypatch.setattr(public_camera, "_ensure_runner_runtime_directory", lambda: None)
    monkeypatch.setattr(public_camera.secrets, "token_hex", lambda _size: "fixed")
    monkeypatch.setattr(public_camera.os, "open", opened)
    monkeypatch.setattr(public_camera.os, "fchown", lambda *args: calls.append(("chown", *args)))
    monkeypatch.setattr(public_camera.os, "fchmod", lambda *args: calls.append(("chmod", *args)))
    monkeypatch.setattr(public_camera.os, "close", lambda *args: calls.append(("close", *args)))

    path = public_camera._create_runner_output(public_camera._RunnerAccount(990, 984))

    assert path.name == "job-fixed.png"
    assert ("chown", 41, 990, 984) in calls
    assert ("chmod", 41, 0o600) in calls
    assert ("close", 41) in calls


def test_runner_output_creation_has_bounded_collision_retries(monkeypatch):
    monkeypatch.setattr(public_camera, "_ensure_runner_runtime_directory", lambda: None)
    monkeypatch.setattr(
        public_camera.os,
        "open",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(FileExistsError),
    )

    with pytest.raises(PublicCameraError, match="runner_output_unavailable"):
        public_camera._create_runner_output(public_camera._RunnerAccount(990, 984))


@pytest.mark.parametrize(
    "result",
    [
        SimpleNamespace(returncode=1, stdout=b'{"ok": true}'),
        SimpleNamespace(returncode=0, stdout=b"not-json"),
        SimpleNamespace(returncode=0, stdout=b'{"ok": false}'),
    ],
)
def test_run_worker_rejects_failed_or_invalid_runner_results(monkeypatch, result):
    monkeypatch.setattr(public_camera.subprocess, "run", lambda *_args, **_kwargs: result)

    with pytest.raises(PublicCameraError, match="public_camera_runner_failed"):
        public_camera._run_worker(["runner"])


def test_run_worker_maps_process_errors_and_accepts_exact_success(monkeypatch):
    monkeypatch.setattr(
        public_camera.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("private diagnostic")),
    )
    with pytest.raises(PublicCameraError, match="public_camera_runner_unavailable"):
        public_camera._run_worker(["runner"])

    monkeypatch.setattr(
        public_camera.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout=b'{"ok": true}'),
    )
    public_camera._run_worker(["runner"])


def test_worker_requires_nonroot_identity(monkeypatch):
    monkeypatch.setattr(public_camera_worker.os, "geteuid", lambda: 0)

    with pytest.raises(public_camera_worker.PublicCameraWorkerError, match="runner_must_not_be_root"):
        public_camera_worker.run_self_test()


def test_worker_validates_private_service_owned_output(monkeypatch, tmp_path):
    output = tmp_path / "frame.png"
    output.touch(mode=0o600)
    real_lstat = Path.lstat

    def lstat(path):
        info = real_lstat(path)
        if path == output:
            return SimpleNamespace(st_mode=info.st_mode, st_uid=990)
        return info

    monkeypatch.setattr(public_camera_worker.os, "geteuid", lambda: 990)
    monkeypatch.setattr(Path, "lstat", lstat)

    public_camera_worker._validate_output(output)


@pytest.mark.parametrize(
    "kind",
    ["root", "missing", "wrong-owner", "wrong-mode"],
)
def test_worker_rejects_unsafe_output_contracts(monkeypatch, tmp_path, kind):
    output = tmp_path / "frame.png"
    if kind != "missing":
        output.touch(mode=0o600)
    runner_uid = 990
    monkeypatch.setattr(public_camera_worker.os, "geteuid", lambda: 0 if kind == "root" else runner_uid)
    if kind == "wrong-owner":
        monkeypatch.setattr(
            Path,
            "lstat",
            lambda _path: SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_uid=runner_uid + 1),
        )
    elif kind == "wrong-mode":
        monkeypatch.setattr(
            Path,
            "lstat",
            lambda _path: SimpleNamespace(st_mode=stat.S_IFREG | 0o644, st_uid=runner_uid),
        )

    with pytest.raises(public_camera_worker.PublicCameraWorkerError):
        public_camera_worker._validate_output(output)


def test_worker_runtime_loader_and_missing_browser_are_safe(monkeypatch, tmp_path):
    assert public_camera_worker._sync_playwright() is not None
    monkeypatch.setattr(public_camera_worker, "PINNED_BROWSER_PATH", tmp_path / "missing-browser")

    with pytest.raises(public_camera_worker.PublicCameraWorkerError, match="browser_not_installed"):
        public_camera_worker._open_browser(SimpleNamespace())


def test_worker_main_reports_successful_self_test(monkeypatch, capsys):
    monkeypatch.setattr(public_camera_worker, "run_self_test", lambda: None)

    assert public_camera_worker.main(["--self-test"]) == 0
    assert json.loads(capsys.readouterr().out) == {"ok": True}


def test_public_camera_cli_can_verify_runner_without_network(monkeypatch, capsys):
    called = 0

    def verify():
        nonlocal called
        called += 1

    monkeypatch.setattr(public_camera, "verify_public_camera_runner", verify)

    assert public_camera.main(["--verify-runner"]) == 0
    assert json.loads(capsys.readouterr().out) == {"ok": True, "runner": "verified"}
    assert called == 1


def test_worker_main_suppresses_unexpected_browser_diagnostics(monkeypatch, capsys):
    monkeypatch.setattr(public_camera_worker, "run_self_test", lambda: (_ for _ in ()).throw(RuntimeError("secret")))

    assert public_camera_worker.main(["--self-test"]) == 2
    assert json.loads(capsys.readouterr().out) == {"ok": False, "error": "public_camera_runner_failed"}


def test_worker_launches_only_pinned_browser(monkeypatch):
    launches: list[dict] = []

    class FakePage:
        def goto(self, *args, **kwargs):
            return None

    class FakeBrowser:
        def new_page(self, **kwargs):
            assert kwargs == {"viewport": {"width": 1280, "height": 720}}
            return FakePage()

        def close(self):
            return None

    class FakeChromium:
        def launch(self, **kwargs):
            launches.append(kwargs)
            return FakeBrowser()

    class FakePlaywrightContext:
        chromium = FakeChromium()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    monkeypatch.setattr(public_camera_worker.os, "geteuid", lambda: 990)
    monkeypatch.setattr(public_camera_worker, "_sync_playwright", lambda: FakePlaywrightContext())
    monkeypatch.setattr(public_camera_worker, "PINNED_BROWSER_PATH", Path("/bin/true"))

    public_camera_worker.run_self_test()

    assert launches == [
        {
            "headless": True,
            "executable_path": str(public_camera_worker.PINNED_BROWSER_PATH),
            "args": ["--no-sandbox", "--disable-gpu"],
        }
    ]


def test_worker_captures_only_allowlisted_provider_frame(monkeypatch, tmp_path):
    output = tmp_path / "frame.png"
    output.touch(mode=0o600)
    evaluations = 0

    class FakePage:
        def __init__(self):
            self.frames = [SimpleNamespace(url="https://fl-4.telecoma.tv/semd185_1/embed.mp4")]

        def goto(self, url, **kwargs):
            assert url == public_camera.CAMERA_PAGE_URL
            assert kwargs["wait_until"] == "domcontentloaded"

        def evaluate(self, _script, argument):
            nonlocal evaluations
            evaluations += 1
            if evaluations == 1:
                assert argument == "/request/camera/url/c_6171"
                return {
                    "overlayTitle": CAMERA_TITLE,
                    "content": '<iframe src="https://fl-4.telecoma.tv/semd185_1/embed.mp4"></iframe>',
                }
            assert argument == "https://fl-4.telecoma.tv/semd185_1/embed.mp4"
            return None

        def wait_for_timeout(self, value):
            assert value == 100

        def screenshot(self, *, path):
            Path(path).write_bytes(b"png")

    class FakeBrowser:
        def new_page(self, **_kwargs):
            return FakePage()

        def close(self):
            return None

    class FakeChromium:
        def launch(self, **_kwargs):
            return FakeBrowser()

    class FakePlaywrightContext:
        chromium = FakeChromium()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    monkeypatch.setattr(public_camera_worker, "_validate_output", lambda path: path.samefile(output))
    monkeypatch.setattr(public_camera_worker, "_sync_playwright", lambda: FakePlaywrightContext())
    monkeypatch.setattr(public_camera_worker, "PINNED_BROWSER_PATH", Path("/bin/true"))

    public_camera_worker.capture_public_camera("semafornaya-185", output, wait_ms=100)

    assert output.read_bytes() == b"png"
    assert evaluations == 2


def test_worker_rejects_invalid_wait(tmp_path):
    with pytest.raises(ValueError, match="wait_ms"):
        public_camera_worker.capture_public_camera("semafornaya-185", tmp_path / "frame.png", wait_ms=15_001)


def test_worker_main_runs_capture_and_suppresses_known_errors(monkeypatch, tmp_path, capsys):
    calls: list[tuple] = []
    output = tmp_path / "frame.png"
    monkeypatch.setattr(
        public_camera_worker,
        "capture_public_camera",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    assert (
        public_camera_worker.main(["--camera-key", "semafornaya-185", "--output", str(output), "--wait-ms", "5"]) == 0
    )
    assert calls == [(("semafornaya-185", output), {"wait_ms": 5})]
    assert json.loads(capsys.readouterr().out) == {"ok": True}

    monkeypatch.setattr(
        public_camera_worker,
        "capture_public_camera",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("private")),
    )
    assert public_camera_worker.main(["--camera-key", "x", "--output", str(output)]) == 2
    assert json.loads(capsys.readouterr().out) == {"ok": False, "error": "public_camera_runner_failed"}


def test_worker_main_requires_capture_arguments():
    with pytest.raises(SystemExit):
        public_camera_worker.main([])


def test_public_camera_cli_lists_resolves_and_captures(monkeypatch, tmp_path, capsys):
    assert public_camera.main(["--list"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert len(listed["cameras"]) == 11

    assert public_camera.main(["--resolve", "Молокова"]) == 0
    resolved = json.loads(capsys.readouterr().out)
    assert resolved["match_count"] == 3

    output = tmp_path / "frame.png"
    monkeypatch.setattr(
        public_camera,
        "capture_public_camera",
        lambda camera_key, output_path, **_kwargs: public_camera.CameraCapture(
            camera_key=camera_key,
            camera_id="c_6171",
            title=CAMERA_TITLE,
            captured_at="2026-08-16T00:00:00+00:00",
            screenshot=str(output_path),
        ),
    )
    assert public_camera.main(["--query", "Автостоп", "--output", str(output)]) == 0
    captured = json.loads(capsys.readouterr().out)
    assert captured["camera_key"] == "semafornaya-185"


def test_controller_rejects_service_owned_symlink_output(tmp_path, root_camera_controller):
    target = tmp_path / "target"
    target.write_bytes(b"keep")
    runner_output = tmp_path / "runner-output.png"
    runner_output.symlink_to(target.name)
    reservation = home_camera._reserve_output(tmp_path / "final.png", overwrite=True)
    account = public_camera._RunnerAccount(uid=os.getuid(), gid=os.getgid())
    try:
        with pytest.raises(PublicCameraError, match="runner_output_invalid"):
            public_camera._copy_verified_runner_output(runner_output, account, reservation)
    finally:
        home_camera._discard_staging_output(reservation)
        os.close(reservation.directory_fd)

    assert target.read_bytes() == b"keep"


@pytest.mark.parametrize(
    "mode,content,width,height,uid_offset",
    [
        (0o600, b"short", 1280, 720, 0),
        (
            0o644,
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + (1280).to_bytes(4, "big") + (720).to_bytes(4, "big"),
            1280,
            720,
            0,
        ),
        (0o600, b"not-a-valid-png-header!!", 1280, 720, 0),
        (
            0o600,
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + (1281).to_bytes(4, "big") + (720).to_bytes(4, "big"),
            1281,
            720,
            0,
        ),
        (
            0o600,
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + (1280).to_bytes(4, "big") + (720).to_bytes(4, "big"),
            1280,
            720,
            1,
        ),
    ],
)
def test_controller_rejects_invalid_runner_png(
    tmp_path, root_camera_controller, mode, content, width, height, uid_offset
):
    del width, height
    source = tmp_path / "runner.png"
    source.write_bytes(content)
    source.chmod(mode)
    reservation = home_camera._reserve_output(tmp_path / "final.png", overwrite=True)
    account = public_camera._RunnerAccount(uid=os.getuid() + uid_offset, gid=os.getgid())
    try:
        with pytest.raises(PublicCameraError, match="runner_output_invalid"):
            public_camera._copy_verified_runner_output(source, account, reservation)
    finally:
        home_camera._discard_staging_output(reservation)
        os.close(reservation.directory_fd)


def test_public_output_reservation_maps_private_output_errors(monkeypatch, tmp_path):
    monkeypatch.setattr(
        home_camera,
        "_reserve_output",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(home_camera.HomeCameraError("output_path_invalid")),
    )

    with pytest.raises(PublicCameraError, match="output_path_invalid"):
        public_camera._reserve_public_output(tmp_path / "frame.png", overwrite=False)


def test_runner_verification_executes_root_sandbox_contract(monkeypatch):
    calls: list[object] = []
    account = public_camera._RunnerAccount(990, 984)
    monkeypatch.setattr(public_camera, "_require_root_controller", lambda: calls.append("root"))
    monkeypatch.setattr(public_camera, "_runner_account", lambda: account)
    monkeypatch.setattr(public_camera, "_ensure_runner_runtime_directory", lambda: calls.append("runtime"))
    monkeypatch.setattr(public_camera, "_run_worker", lambda argv: calls.append(argv))

    public_camera.verify_public_camera_runner()

    assert calls[:2] == ["root", "runtime"]
    assert "--self-test" in calls[2]
    assert "--property=PrivateNetwork=yes" in calls[2]


def test_public_camera_rejects_invalid_wait_before_side_effects(tmp_path):
    with pytest.raises(ValueError, match="wait_ms"):
        public_camera.capture_public_camera("semafornaya-185", tmp_path / "frame.png", wait_ms=-1)


@pytest.mark.parametrize(
    "argv",
    [
        ["--verify-runner", "--list"],
        ["--list", "--resolve", "AutoStop"],
        ["--resolve", "AutoStop", "--camera", "semafornaya-185"],
        ["--query", "AutoStop", "--camera", "semafornaya-185", "--output", "/tmp/frame.png"],
        ["--camera", "semafornaya-185"],
    ],
)
def test_public_camera_cli_rejects_conflicting_or_incomplete_modes(argv):
    with pytest.raises(SystemExit):
        public_camera.main(argv)


def test_public_camera_cli_returns_safe_capture_error(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(
        public_camera,
        "capture_public_camera",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(PublicCameraError("public_camera_runner_failed")),
    )

    assert public_camera.main(["--camera", "semafornaya-185", "--output", str(tmp_path / "frame.png")]) == 2
    assert json.loads(capsys.readouterr().out) == {"ok": False, "error": "public_camera_runner_failed"}


def test_runner_failure_preserves_existing_public_output(monkeypatch, tmp_path, root_camera_controller):
    output = tmp_path / "frame.png"
    output.write_bytes(b"keep")
    account = public_camera._RunnerAccount(uid=os.getuid(), gid=os.getgid())
    monkeypatch.setattr(public_camera, "_runner_account", lambda: account)
    monkeypatch.setattr(public_camera, "_create_runner_output", lambda _account: tmp_path / "runner-output.png")

    def fail(_argv):
        raise PublicCameraError("public_camera_runner_failed")

    monkeypatch.setattr(public_camera, "_run_worker", fail)

    with pytest.raises(PublicCameraError, match="public_camera_runner_failed"):
        public_camera.capture_public_camera("semafornaya-185", output, overwrite=True)

    assert output.read_bytes() == b"keep"
    assert not list(tmp_path.glob(".autostop-camera-*.partial"))


def test_runner_output_creation_failure_discards_reserved_staging(monkeypatch, tmp_path, root_camera_controller):
    output = tmp_path / "frame.png"
    account = public_camera._RunnerAccount(uid=os.getuid(), gid=os.getgid())
    monkeypatch.setattr(public_camera, "_runner_account", lambda: account)
    monkeypatch.setattr(
        public_camera,
        "_create_runner_output",
        lambda _account: (_ for _ in ()).throw(PublicCameraError("public_camera_runner_output_unavailable")),
    )

    with pytest.raises(PublicCameraError, match="public_camera_runner_output_unavailable"):
        public_camera.capture_public_camera("semafornaya-185", output)

    assert not output.exists()
    assert not list(tmp_path.glob(".autostop-camera-*.partial"))


def test_controller_atomically_publishes_verified_runner_png(monkeypatch, tmp_path, root_camera_controller):
    victim = tmp_path / "victim"
    victim.write_bytes(b"keep")
    output = tmp_path / "frame.png"
    output.symlink_to(victim.name)
    runner_output = tmp_path / "runner-output.png"
    runner_output.touch(mode=0o600)
    account = public_camera._RunnerAccount(uid=os.getuid(), gid=os.getgid())
    png_header = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + (1280).to_bytes(4, "big") + (720).to_bytes(4, "big")
    monkeypatch.setattr(public_camera, "_runner_account", lambda: account)
    monkeypatch.setattr(public_camera, "_create_runner_output", lambda _account: runner_output)

    def fake_worker(argv):
        worker_path = Path(argv[argv.index("--output") + 1])
        worker_path.write_bytes(png_header)
        worker_path.chmod(0o600)

    monkeypatch.setattr(public_camera, "_run_worker", fake_worker)

    result = public_camera.capture_public_camera("semafornaya-185", output, overwrite=True)

    assert result.screenshot == str(output)
    assert victim.read_bytes() == b"keep"
    assert not output.is_symlink()
    assert output.read_bytes() == png_header
    assert os.stat(output).st_mode & 0o777 == 0o600
