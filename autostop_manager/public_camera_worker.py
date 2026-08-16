"""Unprivileged browser worker for the one-shot public traffic camera."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import stat

from autostop_manager.public_camera import (
    CAMERA_PAGE_URL,
    PublicCameraError,
    _PLAYER_HOST,
    extract_public_player_url,
    get_public_camera,
)
from urllib.parse import urlparse


PINNED_BROWSER_PATH = Path("/opt/autostop-public-camera-runtime/chromium/chrome-headless-shell")


class PublicCameraWorkerError(RuntimeError):
    """A safe failure from the unprivileged public-camera worker."""


def _validate_output(path: Path) -> None:
    if os.geteuid() == 0:
        raise PublicCameraWorkerError("runner_must_not_be_root")
    try:
        info = path.lstat()
    except OSError as exc:
        raise PublicCameraWorkerError("runner_output_missing") from exc
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
        raise PublicCameraWorkerError("runner_output_invalid")
    if stat.S_IMODE(info.st_mode) != 0o600:
        raise PublicCameraWorkerError("runner_output_permissions_invalid")


def _sync_playwright():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - deployment dependency.
        raise PublicCameraWorkerError("playwright_not_installed") from exc
    return sync_playwright()


def _open_browser(playwright):
    if not PINNED_BROWSER_PATH.is_file() or not os.access(PINNED_BROWSER_PATH, os.X_OK):
        raise PublicCameraWorkerError("browser_not_installed")
    return playwright.chromium.launch(
        headless=True,
        executable_path=str(PINNED_BROWSER_PATH),
        # The browser itself is deliberately launched as a non-root account in
        # a transient systemd sandbox. This bundled headless shell has no usable
        # internal sandbox on this host, so do not make a false sandbox claim.
        args=["--no-sandbox", "--disable-gpu"],
    )


def run_self_test() -> None:
    """Launch only ``about:blank``; used to verify the runner without a camera."""
    if os.geteuid() == 0:
        raise PublicCameraWorkerError("runner_must_not_be_root")
    with _sync_playwright() as playwright:
        browser = _open_browser(playwright)
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 720})
            page.goto("about:blank", wait_until="load", timeout=10_000)
        finally:
            browser.close()


def capture_public_camera(camera_key: str, output_path: Path, *, wait_ms: int) -> None:
    """Render one allowed public frame without accepting a browser path or URL."""
    if not 0 <= wait_ms <= 15_000:
        raise ValueError("wait_ms must be between 0 and 15000")
    camera = get_public_camera(camera_key)
    _validate_output(output_path)
    with _sync_playwright() as playwright:
        browser = _open_browser(playwright)
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 720})
            page.goto(CAMERA_PAGE_URL, wait_until="domcontentloaded", timeout=30_000)
            payload = page.evaluate(
                """async (path) => {
                    const response = await fetch(path);
                    if (!response.ok) throw new Error(`camera_lookup_${response.status}`);
                    return response.json();
                }""",
                f"/request/camera/url/{camera.provider_camera_id}",
            )
            player_url = extract_public_player_url(payload, camera)
            page.evaluate(
                """(url) => {
                    const frame = document.createElement('iframe');
                    frame.src = url;
                    frame.style.cssText = 'position:fixed;inset:0;width:100vw;height:100vh;border:0';
                    document.body.replaceChildren(frame);
                }""",
                player_url,
            )
            page.wait_for_timeout(wait_ms)
            if not any(
                urlparse(frame.url).hostname and _PLAYER_HOST.fullmatch(urlparse(frame.url).hostname or "")
                for frame in page.frames
            ):
                raise PublicCameraWorkerError("camera_frame_not_loaded")
            page.screenshot(path=str(output_path))
        finally:
            browser.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Internal non-root public-camera browser worker.")
    parser.add_argument("--camera-key")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--wait-ms", type=int, default=8_000)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)
    if not args.self_test and (args.output is None or args.camera_key is None):
        parser.error("--camera-key and --output are required unless --self-test is used")
    try:
        if args.self_test:
            run_self_test()
        else:
            capture_public_camera(args.camera_key, args.output, wait_ms=args.wait_ms)
    except (PublicCameraError, PublicCameraWorkerError, ValueError):
        print(json.dumps({"ok": False, "error": "public_camera_runner_failed"}))
        return 2
    except Exception:  # noqa: BLE001 - provider diagnostics may contain dynamic player URLs.
        # Browser diagnostics can contain provider URLs; never let them escape
        # into unit output or logs.
        print(json.dumps({"ok": False, "error": "public_camera_runner_failed"}))
        return 2
    print(json.dumps({"ok": True}))
    return 0


if __name__ == "__main__":  # pragma: no cover - executed by the transient systemd unit.
    raise SystemExit(main())
