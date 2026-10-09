"""Temporary, bounded document extraction for explicit J1 VIN research.

This adapter performs no networking and deliberately does not redact text.  The
caller must establish VIN evidence before redacting/persisting the returned
pages.  Parser input, output and rendered images never leave the supplied
private workspace; parser diagnostics and document content are never logged.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager, suppress
from html.parser import HTMLParser
import math
import os
from pathlib import Path
import pwd
import re
import resource
import shutil
import signal
import stat
import subprocess
import tempfile
import time
from typing import Any
from urllib.parse import urlsplit

MAX_PDF_BYTES = 24 * 1024 * 1024
MAX_HTML_BYTES = 2 * 1024 * 1024
MAX_PAGES = 1000
MAX_TEXT_BYTES = 2 * 1024 * 1024
MAX_PAGE_TEXT_BYTES = 256 * 1024
MAX_OCR_PAGES = 12
MAX_IMAGE_BYTES = 8 * 1024 * 1024
PARSER_MEMORY_BYTES = 256 * 1024 * 1024
PARSER_CPU_SECONDS = 10
PDF_TIMEOUT_SECONDS = 20.0
OCR_TIMEOUT_SECONDS = 60.0
OCR_LANGUAGES = "rus+eng"
_PDF_OUTPUT_BYTES = MAX_TEXT_BYTES + 64 * 1024
_VOID_TAGS = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
)
_HIDDEN_TAGS = frozenset({"script", "style", "template", "noscript", "svg", "head", "title"})
_P_CLOSING_STARTS = frozenset(
    {
        "address",
        "article",
        "aside",
        "blockquote",
        "dd",
        "details",
        "dialog",
        "div",
        "dl",
        "dt",
        "fieldset",
        "figcaption",
        "figure",
        "footer",
        "form",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hgroup",
        "hr",
        "li",
        "main",
        "menu",
        "nav",
        "ol",
        "p",
        "pre",
        "search",
        "section",
        "table",
        "ul",
    }
)
_LI_SCOPE_BOUNDARIES = (_P_CLOSING_STARTS - {"address", "div", "p", "li"}) | frozenset(
    {
        "applet",
        "body",
        "button",
        "caption",
        "center",
        "colgroup",
        "dir",
        "frameset",
        "html",
        "iframe",
        "listing",
        "marquee",
        "noembed",
        "noframes",
        "object",
        "plaintext",
        "select",
        "summary",
        "textarea",
        "tbody",
        "td",
        "tfoot",
        "th",
        "thead",
        "tr",
        "xmp",
    }
)


def _failure(kind: str, method: str, error: str) -> dict[str, Any]:
    return {"ok": False, "kind": kind, "extraction_method": method, "pages": [], "error": error}


def _clip_text(text: str, maximum: int) -> tuple[str, bool]:
    encoded = text.encode("utf-8")
    if len(encoded) <= maximum:
        return text, False
    return encoded[: max(0, maximum)].decode("utf-8", "ignore"), True


def _bounded_timeout(default: float, remaining: float | None) -> float:
    if remaining is None:
        return default
    if type(remaining) not in {int, float} or not math.isfinite(remaining) or remaining <= 0:
        return 0.0
    return min(default, float(remaining))


class _VisibleHTML(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, bool]] = []
        self.parts: list[str] = []
        self.title_parts: list[str] = []
        self.remaining = MAX_TEXT_BYTES
        self.title_remaining = 1024
        self.truncated = False
        self.structure_limited = False

    def _close_in_scope(self, tags: set[str], boundaries: set[str] | frozenset[str]) -> None:
        # HTMLParser emits tokens rather than a browser DOM.  Omitted end tags
        # close siblings, but must not cross nested lists/tables or hidden
        # parsing contexts and accidentally expose their contents.
        for index in range(len(self.stack) - 1, -1, -1):
            tag = self.stack[index][0]
            if tag in tags:
                del self.stack[index:]
                return
            if tag in boundaries or tag in _HIDDEN_TAGS:
                return

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.structure_limited:
            return
        if tag in _P_CLOSING_STARTS:
            self._close_in_scope(
                {"p"}, {"html", "table", "td", "th", "caption", "button", "applet", "object", "marquee"}
            )
        if tag == "li":
            self._close_in_scope({"li"}, _LI_SCOPE_BOUNDARIES)
        elif tag in {"td", "th"}:
            self._close_in_scope({"td", "th"}, {"tr", "table", "thead", "tbody", "tfoot"})
        elif tag == "tr":
            self._close_in_scope({"tr"}, {"table", "thead", "tbody", "tfoot"})
        elif tag in {"thead", "tbody", "tfoot"}:
            self._close_in_scope({"tr"}, {"table", "thead", "tbody", "tfoot"})
            self._close_in_scope({"thead", "tbody", "tfoot"}, {"table"})
        if len(self.stack) >= 512:
            self.structure_limited = True
            self.truncated = True
            return
        values = dict(attrs)
        style = re.sub(r"\s+", "", (values.get("style") or "").lower())
        hidden = (
            tag in _HIDDEN_TAGS
            or "hidden" in values
            or (values.get("aria-hidden") or "").lower() == "true"
            or "display:none" in style
            or "visibility:hidden" in style
            or bool(self.stack and self.stack[-1][1])
        )
        if tag not in _VOID_TAGS:
            self.stack.append((tag, hidden))
        if not hidden and tag in {"p", "br", "div", "li", "tr", "td", "th", "h1", "h2", "h3", "h4", "hr"}:
            self._append("\n")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in _VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if self.structure_limited:
            return
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break
        if tag in {"p", "div", "li", "tr"} and not (self.stack and self.stack[-1][1]):
            self._append("\n")

    def _append(self, text: str) -> None:
        part, clipped = _clip_text(text, self.remaining)
        self.parts.append(part)
        self.remaining -= len(part.encode("utf-8"))
        self.truncated |= clipped

    def handle_data(self, data: str) -> None:
        if self.structure_limited:
            return
        if any(tag == "title" for tag, _hidden in self.stack):
            part, _clipped = _clip_text(data, self.title_remaining)
            self.title_parts.append(part)
            self.title_remaining -= len(part.encode("utf-8"))
        if not (self.stack and self.stack[-1][1]):
            self._append(data)


def _html_content(body: bytes, content_type: str) -> dict[str, Any]:
    if len(body) > MAX_HTML_BYTES:
        return _failure("html", "html_text", "document_body_too_large")
    charset_match = re.search(r"charset\s*=\s*[\"']?([\w.-]+)", content_type, re.I)
    charset = charset_match.group(1) if charset_match else "utf-8"
    try:
        decoded = body.decode(charset, "replace")
    except LookupError:
        decoded = body.decode("utf-8", "replace")
    parser = _VisibleHTML()
    parser.feed(decoded)
    parser.close()
    text = re.sub(r"[ \t]+", " ", "".join(parser.parts))
    text = re.sub(r"\n\s*\n", "\n", text).strip()
    limitations = ["static_html_visibility"]
    if parser.truncated:
        limitations.append("text_limit")
    if parser.structure_limited:
        limitations.append("html_structure_limit")
    return {
        "ok": True,
        "kind": "html",
        "extraction_method": "html_text",
        "title": " ".join("".join(parser.title_parts).split()),
        "pages": [{"page": 1, "text": text}],
        "page_count": 1,
        "truncated": parser.truncated,
        "limitations": limitations,
    }


def _restrict_child(file_limit: int) -> None:
    resource.setrlimit(resource.RLIMIT_AS, (PARSER_MEMORY_BYTES, PARSER_MEMORY_BYTES))
    resource.setrlimit(resource.RLIMIT_CPU, (PARSER_CPU_SECONDS, PARSER_CPU_SECONDS))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    resource.setrlimit(resource.RLIMIT_FSIZE, (file_limit, file_limit))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    if os.geteuid() == 0:
        nobody = pwd.getpwnam("nobody")
        os.setgroups([])
        os.setgid(nobody.pw_gid)
        os.setuid(nobody.pw_uid)


@contextmanager
def _parser_workspace(workspace: Path) -> Iterator[tuple[Path, int]]:
    """Keep root-owned paths private; children use only inherited I/O FDs.

    The production worker has SETUID/SETGID but no CHOWN, FOWNER or DAC
    capabilities.  Never transfer directory ownership: parsers cannot create
    paths here after dropping to nobody, and the parent can remove its own
    artifacts without additional capabilities.
    """
    workspace = Path(workspace).absolute()
    if workspace.resolve() != workspace:
        raise ValueError("workspace_unavailable")
    info = workspace.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError("workspace_unavailable")
    with tempfile.TemporaryDirectory(prefix="j1-vin-parser-", dir=workspace) as raw_dir:
        directory = Path(raw_dir)
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fchmod(descriptor, 0o700)
            yield directory, descriptor
        finally:
            os.close(descriptor)


def _kill_parser(process: subprocess.Popen[bytes]) -> None:
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    process.communicate(timeout=2)


def _run_parser(
    argv: list[str],
    directory: Path,
    descriptor: int,
    *,
    payload: bytes | None = None,
    timeout: float,
    output_limit: int,
    file_limit: int | None = None,
) -> tuple[str, bytes]:
    if timeout <= 0 or not math.isfinite(timeout):
        return "timeout", b""
    try:
        with tempfile.TemporaryFile(dir=directory) as output:
            process = subprocess.Popen(
                argv,
                cwd=f"/proc/self/fd/{descriptor}",
                pass_fds=(descriptor,),
                stdin=subprocess.PIPE if payload is not None else subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                preexec_fn=lambda: _restrict_child(file_limit or output_limit),
                env={
                    "PATH": "/usr/bin:/bin",
                    "LANG": "C.UTF-8",
                    "LC_ALL": "C.UTF-8",
                    "HOME": ".",
                    "TMPDIR": ".",
                    "XDG_CACHE_HOME": ".",
                },
            )
            try:
                process.communicate(input=payload, timeout=timeout)
            except subprocess.TimeoutExpired:
                _kill_parser(process)
                return "timeout", b""
            output.seek(0)
            content = output.read(output_limit)
            if len(content) >= output_limit:
                return "output_limit", content
            return ("ok" if process.returncode == 0 else "failed"), content
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        return "unavailable", b""


def _pdf_page_count(body: bytes, directory: Path, descriptor: int, deadline: float) -> int | None:
    binary = shutil.which("pdfinfo")
    if not binary:
        return None
    outcome, output = _run_parser(
        [binary, "-"],
        directory,
        descriptor,
        payload=body,
        timeout=min(5.0, deadline - time.monotonic()),
        output_limit=32 * 1024,
    )
    match = re.search(rb"(?m)^Pages:\s*(\d+)\s*$", output) if outcome == "ok" else None
    return int(match.group(1)) if match else None


def _pdf_pages(output: bytes) -> tuple[list[dict[str, Any]], bool, list[str]]:
    chunks = output.decode("utf-8", "replace").split("\f")
    if chunks[-1] == "":
        chunks.pop()
    pages: list[dict[str, Any]] = []
    remaining = MAX_TEXT_BYTES
    limitations: set[str] = set()
    if len(chunks) > MAX_PAGES:
        limitations.add("page_limit")
    for number, chunk in enumerate(chunks[:MAX_PAGES], 1):
        text, clipped = _clip_text(chunk.strip(), min(MAX_PAGE_TEXT_BYTES, remaining))
        remaining -= len(text.encode("utf-8"))
        row: dict[str, Any] = {"page": number, "text": text}
        if clipped:
            row["truncated"] = True
            limitations.add("text_limit" if remaining == 0 else "page_text_limit")
        pages.append(row)
    return pages, bool(limitations), sorted(limitations)


def _pdf_content(body: bytes, workspace: Path, timeout_seconds: float | None = None) -> dict[str, Any]:
    if len(body) > MAX_PDF_BYTES:
        return _failure("pdf", "pdf_text", "document_body_too_large")
    if not body.startswith(b"%PDF"):
        return _failure("pdf", "pdf_text", "invalid_pdf")
    budget = _bounded_timeout(PDF_TIMEOUT_SECONDS, timeout_seconds)
    if not budget:
        return _failure("pdf", "pdf_text", "pdf_timeout")
    deadline = time.monotonic() + budget
    binary = shutil.which("pdftotext")
    if not binary:
        return _failure("pdf", "pdf_text", "pdf_tool_unavailable")
    try:
        with _parser_workspace(workspace) as (directory, descriptor):
            page_count = _pdf_page_count(body, directory, descriptor, deadline)
            outcome, output = _run_parser(
                [binary, "-f", "1", "-l", str(MAX_PAGES), "-layout", "-enc", "UTF-8", "-", "-"],
                directory,
                descriptor,
                payload=body,
                timeout=deadline - time.monotonic(),
                output_limit=_PDF_OUTPUT_BYTES,
            )
    except (OSError, ValueError, KeyError):
        return _failure("pdf", "pdf_text", "workspace_unavailable")
    if outcome not in {"ok", "output_limit"}:
        code = "pdf_timeout" if outcome == "timeout" else "pdf_extract_failed"
        return _failure("pdf", "pdf_text", code)
    pages, truncated, limitations = _pdf_pages(output)
    if outcome == "output_limit":
        truncated = True
        limitations.append("text_limit")
    if page_count is not None and page_count > MAX_PAGES:
        truncated = True
        limitations.append("page_limit")
    if page_count is None:
        limitations.append("page_count_inferred")
    if not any(page["text"] for page in pages):
        limitations.append("no_extractable_text")
    return {
        "ok": True,
        "kind": "pdf",
        "extraction_method": "pdf_text",
        "pages": pages,
        "page_count": page_count if page_count is not None else len(pages),
        "truncated": truncated,
        "limitations": sorted(set(limitations)),
    }


def extract_content(
    body: bytes,
    content_type: str,
    url: str,
    workspace: Path,
    *,
    timeout_seconds: float | None = None,
) -> dict[str, Any]:
    """Return page-indexed text before VIN redaction, without network access.

    The network caller must apply its ordinary 8 MiB PDF limit; the 24 MiB
    allowance here is only for explicitly registered A/B domains.  The caller
    supplies a private ephemeral workspace (production uses root-only /run).
    """
    if not isinstance(body, bytes):
        return _failure("unknown", "none", "invalid_body")
    media_type = str(content_type).split(";", 1)[0].strip().lower()
    try:
        pdf_suffix = urlsplit(url).path.lower().endswith(".pdf")
    except ValueError:
        pdf_suffix = False
    if body.startswith(b"%PDF") or media_type == "application/pdf" or pdf_suffix:
        return _pdf_content(body, workspace, timeout_seconds)
    if media_type in {"", "text/html", "application/xhtml+xml"}:
        if not _bounded_timeout(PDF_TIMEOUT_SECONDS, timeout_seconds):
            return _failure("html", "html_text", "document_timeout")
        return _html_content(body, content_type)
    return _failure("unknown", "none", "unsupported_content_type")


def _ocr_selection(pages: list[int]) -> list[int] | None:
    if not isinstance(pages, list) or not pages or len(pages) > MAX_OCR_PAGES:
        return None
    if any(type(page) is not int or page < 1 or page > MAX_PAGES for page in pages):
        return None
    if len(set(pages)) != len(pages):
        return None
    return pages


def _ocr_page(
    body: bytes,
    page: int,
    directory: Path,
    descriptor: int,
    deadline: float,
    render_binary: str,
    ocr_binary: str,
) -> tuple[str, str, bool]:
    # With no output prefix pdftoppm writes the single PNG to stdout.  The
    # parent's root-owned 0600 tempfile receives it through an inherited FD.
    # Tesseract consumes those bytes from stdin, so neither program needs
    # access to a child-owned path underneath the private job directory.
    outcome, image = _run_parser(
        [
            render_binary,
            "-f",
            str(page),
            "-l",
            str(page),
            "-singlefile",
            "-scale-to",
            "2400",
            "-r",
            "150",
            "-png",
            "-",
        ],
        directory,
        descriptor,
        payload=body,
        timeout=min(15.0, deadline - time.monotonic()),
        output_limit=MAX_IMAGE_BYTES,
        file_limit=MAX_IMAGE_BYTES,
    )
    if outcome != "ok":
        return ("ocr_timeout" if outcome == "timeout" else "ocr_render_failed"), "", False
    if not image.startswith(b"\x89PNG\r\n\x1a\n"):
        return "ocr_render_failed", "", False
    outcome, output = _run_parser(
        [ocr_binary, "stdin", "stdout", "-l", OCR_LANGUAGES, "--psm", "3"],
        directory,
        descriptor,
        payload=image,
        timeout=min(10.0, deadline - time.monotonic()),
        output_limit=MAX_PAGE_TEXT_BYTES + 1,
    )
    if outcome not in {"ok", "output_limit"}:
        return ("ocr_timeout" if outcome == "timeout" else "ocr_extract_failed"), "", False
    text, clipped = _clip_text(output.decode("utf-8", "replace").strip(), MAX_PAGE_TEXT_BYTES)
    return "", text, clipped or outcome == "output_limit"


def ocr_pages(
    body: bytes,
    pages: list[int],
    workspace: Path,
    *,
    timeout_seconds: float | None = None,
) -> dict[str, Any]:
    """OCR only selected pages; the caller enforces the twelve-page job budget."""
    selected = _ocr_selection(pages)
    if selected is None:
        return _failure("pdf", "pdf_ocr", "ocr_invalid_pages")
    if not isinstance(body, bytes) or not body.startswith(b"%PDF"):
        return _failure("pdf", "pdf_ocr", "invalid_pdf")
    if len(body) > MAX_PDF_BYTES:
        return _failure("pdf", "pdf_ocr", "document_body_too_large")
    budget = _bounded_timeout(OCR_TIMEOUT_SECONDS, timeout_seconds)
    if not budget:
        return _failure("pdf", "pdf_ocr", "ocr_timeout")
    deadline = time.monotonic() + budget
    render_binary, ocr_binary = shutil.which("pdftoppm"), shutil.which("tesseract")
    if not render_binary or not ocr_binary:
        return _failure("pdf", "pdf_ocr", "ocr_tool_unavailable")
    result_pages: list[dict[str, Any]] = []
    limitations: set[str] = set()
    remaining = MAX_TEXT_BYTES
    try:
        with _parser_workspace(workspace) as (directory, descriptor):
            outcome, languages = _run_parser(
                [ocr_binary, "--list-langs"],
                directory,
                descriptor,
                timeout=min(5.0, deadline - time.monotonic()),
                output_limit=16 * 1024,
            )
            if outcome != "ok":
                return _failure("pdf", "pdf_ocr", "ocr_timeout" if outcome == "timeout" else "ocr_tool_unavailable")
            if not {"rus", "eng"}.issubset(set(languages.decode("utf-8", "replace").splitlines())):
                return _failure("pdf", "pdf_ocr", "ocr_languages_unavailable")
            for page in selected:
                error, text, clipped = _ocr_page(body, page, directory, descriptor, deadline, render_binary, ocr_binary)
                if error:
                    result = _failure("pdf", "pdf_ocr", error)
                    result["pages"] = result_pages
                    result["limitations"] = ["partial_ocr"] if result_pages else []
                    return result
                text, aggregate_clipped = _clip_text(text, remaining)
                remaining -= len(text.encode("utf-8"))
                row: dict[str, Any] = {"page": page, "text": text}
                if clipped or aggregate_clipped:
                    row["truncated"] = True
                    limitations.add("text_limit" if aggregate_clipped else "page_text_limit")
                result_pages.append(row)
    except (OSError, ValueError, KeyError):
        return _failure("pdf", "pdf_ocr", "workspace_unavailable")
    if not any(row["text"] for row in result_pages):
        limitations.add("no_extractable_text")
    return {
        "ok": True,
        "kind": "pdf",
        "extraction_method": "pdf_ocr",
        "pages": result_pages,
        "pages_processed": len(result_pages),
        "truncated": bool(limitations - {"no_extractable_text"}),
        "limitations": sorted(limitations),
    }


__all__ = ["OCR_LANGUAGES", "extract_content", "ocr_pages"]
