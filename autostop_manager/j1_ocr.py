"""Bounded OCR fallback for scanned public PDFs in the J1 worker.

It retains neither the input PDF nor rendered page images.  All parser and OCR
children run with reduced privileges where possible and conservative resource
limits.  The caller decides when this fallback is appropriate (normally only
after regular ``pdftotext`` returned no text).
"""

from __future__ import annotations

from functools import lru_cache
import os
from pathlib import Path
import pwd
import resource
import shutil
import subprocess
import tempfile
import time
from typing import Any

from .j1_fetch import MAX_TEXT_CHARS, redact_sensitive


MAX_PDF_BYTES = 8_000_000
MAX_PAGES = 10
MAX_TIMEOUT_SECONDS = 30
MAX_IMAGE_BYTES = 30_000_000
MAX_IMAGE_FILE_BYTES = MAX_IMAGE_BYTES // MAX_PAGES
OCR_LANGUAGES = "rus+eng"


def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        numeric = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return max(minimum, min(numeric, maximum))


def _restrict_child() -> None:
    resource.setrlimit(resource.RLIMIT_AS, (256 * 1024 * 1024, 256 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_CPU, (10, 10))
    resource.setrlimit(resource.RLIMIT_NOFILE, (32, 32))
    # ``pdftoppm`` produces one file per page.  Keep each image under a tenth
    # of the aggregate cap so temporary OCR input cannot exceed the contract.
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_IMAGE_FILE_BYTES, MAX_IMAGE_FILE_BYTES))
    if os.geteuid() == 0:
        nobody = pwd.getpwnam("nobody")
        os.setgroups([])
        os.setgid(nobody.pw_gid)
        os.setuid(nobody.pw_uid)


def _binaries_available() -> bool:
    return bool(shutil.which("pdftoppm") and shutil.which("tesseract"))


@lru_cache(maxsize=1)
def _ocr_languages_available() -> bool:
    if not _binaries_available():
        return False
    try:
        result = subprocess.run(
            ["tesseract", "--list-langs"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    languages = set(result.stdout.decode("utf-8", "replace").splitlines())
    return result.returncode == 0 and {"rus", "eng"}.issubset(languages)


def _run(argv: list[str], *, timeout: float, stdout: Any = subprocess.DEVNULL) -> bool:
    try:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=subprocess.DEVNULL,
            preexec_fn=_restrict_child,
        )
        try:
            process.wait(timeout=max(1.0, timeout))
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)
            return False
    except (OSError, subprocess.SubprocessError, KeyError):
        return False
    return process.returncode == 0


def _prepare_workdir(path: Path, source: Path) -> None:
    if os.geteuid() != 0:
        return
    nobody = pwd.getpwnam("nobody")
    os.chown(path, nobody.pw_uid, nobody.pw_gid)
    os.chmod(path, 0o700)
    # The parent creates the PDF before dropping the child.  Transfer both
    # directory and file ownership, otherwise a 0600 root file is unreadable
    # to the intentionally unprivileged parser.
    os.chown(source, nobody.pw_uid, nobody.pw_gid)
    os.chmod(source, 0o600)


def _failure(code: str, *, retryable: bool) -> dict[str, Any]:
    return {"ok": False, "error": code, "retryable": retryable, "extraction_method": "pdf_ocr"}


def extract_scanned_pdf(
    body: bytes,
    *,
    max_pages: int = MAX_PAGES,
    max_chars: int = MAX_TEXT_CHARS,
    timeout_seconds: int = MAX_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Extract text from up to ten scanned PDF pages without retaining files."""

    if not isinstance(body, bytes) or not body.startswith(b"%PDF") or len(body) > MAX_PDF_BYTES:
        return _failure("ocr_invalid_pdf", retryable=False)
    if not _ocr_languages_available():
        return _failure("ocr_unavailable", retryable=True)
    pages = _bounded_int(max_pages, default=MAX_PAGES, minimum=1, maximum=MAX_PAGES)
    char_limit = _bounded_int(max_chars, default=MAX_TEXT_CHARS, minimum=80, maximum=MAX_TEXT_CHARS)
    timeout = _bounded_int(timeout_seconds, default=MAX_TIMEOUT_SECONDS, minimum=3, maximum=MAX_TIMEOUT_SECONDS)
    deadline = time.monotonic() + timeout
    try:
        with tempfile.TemporaryDirectory(prefix="j1-ocr-") as raw_dir:
            workdir = Path(raw_dir)
            source = workdir / "source.pdf"
            source.write_bytes(body)
            os.chmod(source, 0o600)
            _prepare_workdir(workdir, source)
            prefix = workdir / "page"
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not _run(
                [
                    "pdftoppm",
                    "-f",
                    "1",
                    "-l",
                    str(pages),
                    "-r",
                    "200",
                    "-jpeg",
                    "-jpegopt",
                    "quality=85",
                    str(source),
                    str(prefix),
                ],
                timeout=min(15.0, remaining),
            ):
                return _failure("ocr_render_failed", retryable=True)
            images = sorted(workdir.glob("page-*.jpg"))[:pages]
            if not images or sum(image.stat().st_size for image in images) > MAX_IMAGE_BYTES:
                return _failure("ocr_render_failed", retryable=True)
            parts: list[str] = []
            for image in images:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return _failure("ocr_timeout", retryable=True)
                with tempfile.TemporaryFile() as output:
                    if not _run(
                        ["tesseract", str(image), "stdout", "-l", OCR_LANGUAGES, "--psm", "3"],
                        timeout=min(5.0, remaining),
                        stdout=output,
                    ):
                        return _failure("ocr_extract_failed", retryable=True)
                    output.seek(0)
                    parts.append(output.read(char_limit * 2).decode("utf-8", "replace"))
                if sum(len(part) for part in parts) >= char_limit:
                    break
    except (OSError, ValueError):
        return _failure("ocr_extract_failed", retryable=True)
    text = redact_sensitive("\n".join(parts), limit=char_limit)
    if len(text) < 80:
        return _failure("ocr_empty", retryable=False)
    return {
        "ok": True,
        "text": text,
        "kind": "pdf",
        "extraction_method": "pdf_ocr",
        "pages_processed": len(images),
    }


__all__ = ["OCR_LANGUAGES", "extract_scanned_pdf"]
