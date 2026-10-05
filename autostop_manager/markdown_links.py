"""Extract navigable Markdown destinations using CommonMark with tables.

Only direct inline link tokens count. Image children describe alt text and must
not be traversed; code, HTML literals and unused reference definitions do not
create navigation. The parser does not render HTML or access target files.
"""

from __future__ import annotations

from markdown_it import MarkdownIt

_MARKDOWN = MarkdownIt("commonmark").enable("table")


def visible_markdown_links(text: str) -> list[str]:
    """Return actual link destinations in document order, with no I/O."""
    links: list[str] = []
    for token in _MARKDOWN.parse(text):
        if token.type != "inline":
            continue
        for child in token.children or ():
            if child.type == "link_open":
                href = child.attrGet("href")
                if isinstance(href, str):
                    links.append(href)
    return links
