"""Normalize local document destinations without resolving their access scope.

App file links accept an optional positive ASCII decimal ``:line`` suffix,
including leading zeros. URI schemes and authorities remain external. A bare
basename followed by a number is ambiguous with a URI, so only the explicit
file extensions below use the file-link interpretation; other basenames need
``./`` or an absolute path. Decode the path once, after separating URI fields.
Consumers retain their existing containment, symlink and external allowlists.
"""

from __future__ import annotations

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


def local_document_link_target(link: str) -> str | None:
    """Return a decoded local path; reject invalid numeric line annotations.

    No filesystem reads or external-resource checks take place here. A
    nonnumeric colon suffix remains an ordinary path or URI, not a line number.
    """
    parsed = urlsplit(link)
    if parsed.netloc or not parsed.path:
        return None
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
        return path
    return target
