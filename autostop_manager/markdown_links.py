"""Extract navigable Markdown destinations using CommonMark with tables.

Only direct inline link tokens count. Image children describe alt text and must
not be traversed; code, HTML literals and unused reference definitions do not
create navigation. The parser does not render HTML or access target files.
"""

from __future__ import annotations

from functools import lru_cache

from markdown_it import MarkdownIt

_MARKDOWN = MarkdownIt("commonmark").enable("table")


@lru_cache(maxsize=256)
def _navigation_metadata(text: str) -> tuple[tuple[str, ...], frozenset[int]]:
    """Cache only pure parsing of exact content; filesystem checks stay fresh."""
    links: list[str] = []
    row_lines: set[int] = set()
    for token in _MARKDOWN.parse(text):
        if token.type == "tr_open" and token.map:
            row_lines.add(token.map[0])
        if token.type != "inline":
            continue
        for child in token.children or ():
            if child.type == "link_open":
                href = child.attrGet("href")
                if isinstance(href, str):
                    links.append(href)
    return tuple(links), frozenset(row_lines)


def visible_markdown_links(text: str) -> list[str]:
    """Return a fresh list of actual destinations in document order, with no I/O."""
    return list(_navigation_metadata(text)[0])


def visible_markdown_table_row_lines(text: str) -> frozenset[int]:
    """Return zero-based source lines of actual table rows, excluding literals."""
    return _navigation_metadata(text)[1]
