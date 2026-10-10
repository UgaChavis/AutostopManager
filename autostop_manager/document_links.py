"""Normalize local document destinations without resolving their access scope.

App file links accept an optional positive ASCII decimal ``:line`` suffix,
including leading zeros. URI schemes and authorities remain external. A bare
basename followed by a number is ambiguous with a URI, so only the explicit
file extensions below use the file-link interpretation; other basenames need
``./`` or an absolute path. Decode the path once, after separating URI fields.
Consumers retain their existing containment, symlink and external allowlists.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit

BARE_FILE_LINK_EXTENSIONS = frozenset(
    {
        ".md",
        ".markdown",
        ".json",
        ".jsonl",
        ".py",
        ".pyi",
        ".toml",
        ".yaml",
        ".yml",
        ".txt",
        ".rst",
        ".sh",
        ".bash",
        ".js",
        ".jsx",
        ".ts",
        ".tsx",
        ".html",
        ".htm",
        ".css",
        ".scss",
        ".sql",
        ".xml",
        ".csv",
        ".ini",
        ".cfg",
        ".conf",
    }
)


@dataclass(frozen=True)
class LocalDocumentLink:
    path: str
    line: int | None = None
    fragment: str = ""


def parse_local_document_link(link: str) -> LocalDocumentLink | None:
    """Keep local file, line and fragment separate; leave external URIs alone.

    No filesystem reads or external-resource checks take place here. A
    nonnumeric colon suffix remains an ordinary path or URI, not a line number.
    """
    parsed = urlsplit(link)
    if parsed.netloc or (not parsed.path and not parsed.fragment):
        return None
    fragment = unquote(parsed.fragment)
    if not parsed.path:
        return LocalDocumentLink("", fragment=fragment)
    if parsed.scheme:
        basename = link.partition(":")[0]
        line = unquote(parsed.path)
        if Path(basename).suffix.lower() not in BARE_FILE_LINK_EXTENSIONS or not re.fullmatch(r"[+-]?\d+", line):
            return None
        target = basename + ":" + line
    else:
        target = unquote(parsed.path)
    path, separator, line = target.rpartition(":")
    if separator and re.fullmatch(r"[+-]?\d+", line):
        if not re.fullmatch(r"[0-9]+", line) or not line.strip("0"):
            raise ValueError("document_link_line_invalid")
        return LocalDocumentLink(path, int(line.lstrip("0")), fragment)
    return LocalDocumentLink(target, fragment=fragment)


def local_document_link_target(link: str) -> str | None:
    """Return the local filename, preserving the existing path-only interface."""
    parsed = parse_local_document_link(link)
    return parsed.path if parsed is not None and parsed.path else None


def validate_document_reference(link: LocalDocumentLink, target: Path, text: str) -> None:
    """Validate a reference after its consumer has approved and read the file."""
    if link.line is not None and link.line > len(text.splitlines()):
        raise ValueError("document_link_line_out_of_range")
    if link.fragment and target.suffix.lower() in {".md", ".markdown"}:
        # Keep Markdown dependencies out of the stdlib-only Telegram lane.
        from .markdown_links import visible_markdown_anchors

        if link.fragment not in visible_markdown_anchors(text):
            raise ValueError("document_link_anchor_missing")
