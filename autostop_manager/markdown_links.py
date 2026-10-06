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
def _link_destinations(text: str) -> tuple[str, ...]:
    """Cache only pure parsing of exact content; filesystem checks stay fresh."""
    links: list[str] = []
    for token in _MARKDOWN.parse(text):
        if token.type != "inline":
            continue
        for child in token.children or ():
            if child.type == "link_open":
                href = child.attrGet("href")
                if isinstance(href, str):
                    links.append(href)
    return tuple(links)


def visible_markdown_links(text: str) -> list[str]:
    """Return a fresh list of actual destinations in document order, with no I/O."""
    return list(_link_destinations(text))
