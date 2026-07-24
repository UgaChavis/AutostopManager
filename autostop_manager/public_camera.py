"""Bounded public-camera capture for the AutoStop Semafornaya 185 traffic view."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import re
from typing import Any
from urllib.parse import urlparse


CAMERA_ID = "c_6171"
CAMERA_TITLE = "Семафорная 185"
CAMERA_PAGE_URL = "https://24oko.ru/city"
CAMERA_PLAYER_PATH = f"/request/camera/url/{CAMERA_ID}"
_PLAYER_HOST = re.compile(r"^fl-[0-9]+\.telecoma\.tv$", re.IGNORECASE)


class PublicCameraError(RuntimeError):
    """The public source did not provide the expected camera payload."""


@dataclass(frozen=True)
class CameraCapture:
    camera_id: str
    title: str
    captured_at: str
    screenshot: str


def extract_public_player_url(payload: dict[str, Any]) -> str:
    """Validate the provider payload before embedding its current player URL."""
    if payload.get("overlayTitle") != CAMERA_TITLE:
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
        or not parsed.path.endswith("/embed.mp4")
    ):
        raise PublicCameraError("unexpected_camera_player")
    return player_url


def _browser_candidates(playwright_path: str, requested_path: str | None) -> list[Path]:
    candidates = [Path(requested_path)] if requested_path else []
    configured_path = os.environ.get("AUTOSTOP_CAMERA_BROWSER")
    if configured_path:
        candidates.append(Path(configured_path))
    candidates.append(Path(playwright_path))
    candidates.extend(Path.home().glob(".cache/ms-playwright/chromium-*/chrome-linux*/chrome"))
    return candidates


def _select_browser(playwright_path: str, requested_path: str | None) -> str | None:
    for candidate in _browser_candidates(playwright_path, requested_path):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def capture_semafornaya_185(
    output_path: Path,
    *,
    overwrite: bool = False,
    wait_ms: int = 8_000,
    browser_path: str | None = None,
) -> CameraCapture:
    """Save one current public frame; never poll, archive, or identify people."""
    if not 0 <= wait_ms <= 15_000:
        raise ValueError("wait_ms must be between 0 and 15000")
    if not output_path.parent.is_dir():
        raise FileNotFoundError(f"output directory does not exist: {output_path.parent}")
    if output_path.exists() and not overwrite:
        raise FileExistsError(f"refusing to overwrite existing file: {output_path}")

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - depends on optional runtime tooling.
        raise PublicCameraError("playwright_not_installed") from exc

    with sync_playwright() as playwright:
        executable_path = _select_browser(playwright.chromium.executable_path, browser_path)
        if executable_path is None:
            raise PublicCameraError("browser_not_installed")
        browser = playwright.chromium.launch(
            headless=True,
            executable_path=executable_path,
            args=["--no-sandbox"],
        )
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 720})
            page.goto(CAMERA_PAGE_URL, wait_until="domcontentloaded", timeout=30_000)
            payload = page.evaluate(
                """async (path) => {
                    const response = await fetch(path);
                    if (!response.ok) throw new Error(`camera_lookup_${response.status}`);
                    return response.json();
                }""",
                CAMERA_PLAYER_PATH,
            )
            player_url = extract_public_player_url(payload)
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
                raise PublicCameraError("camera_frame_not_loaded")
            page.screenshot(path=str(output_path))
        finally:
            browser.close()

    return CameraCapture(
        camera_id=CAMERA_ID,
        title=CAMERA_TITLE,
        captured_at=datetime.now(UTC).isoformat(),
        screenshot=str(output_path),
    )


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Capture one current public frame from Semafornaya 185.")
    parser.add_argument(
        "--output", required=True, type=Path, help="Exact output PNG path; parent directory must exist."
    )
    parser.add_argument("--overwrite", action="store_true", help="Allow replacing the exact output file.")
    parser.add_argument("--wait-ms", type=int, default=8_000, help="Single render wait, from 0 to 15000 ms.")
    parser.add_argument("--browser-path", help="Optional exact Chromium executable path.")
    args = parser.parse_args(argv)
    result = capture_semafornaya_185(
        args.output,
        overwrite=args.overwrite,
        wait_ms=args.wait_ms,
        browser_path=args.browser_path,
    )
    print(json.dumps(asdict(result), ensure_ascii=False))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised as a standalone operational helper.
    raise SystemExit(main())
