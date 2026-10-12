"""Extract navigable Markdown destinations using CommonMark with tables.

Only direct inline link tokens count. Image children describe alt text and must
not be traversed; code, HTML literals and unused reference definitions do not
create navigation. Leading YAML front matter is metadata, with its source line
numbers preserved for the body. The parser does not render HTML or access files.
"""

from __future__ import annotations

from functools import lru_cache
from html.parser import HTMLParser
import unicodedata

from markdown_it import MarkdownIt
from markdown_it.token import Token

from .document_links import document_source_lines

_MARKDOWN = MarkdownIt("commonmark").enable("table")


def _markdown_body(text: str) -> str:
    """Hide a closed leading YAML block without shifting CommonMark source maps."""
    if not text.startswith("---"):
        return text
    # Match the parser's newline normalization, keeping Unicode separators in
    # literal text. YAML is metadata even when its values contain Markdown.
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if lines[0].rstrip(" \t") != "---":
        return text
    for index in range(1, len(lines)):
        if lines[index].rstrip(" \t") == "---":
            return "\n" * (index + 1) + "\n".join(lines[index + 1 :])
    return text


@lru_cache(maxsize=256)
def _navigation_metadata(text: str) -> tuple[tuple[str, ...], frozenset[int]]:
    """Cache only pure parsing of exact content; filesystem checks stay fresh."""
    links: list[str] = []
    row_lines: set[int] = set()
    for token in _MARKDOWN.parse(_markdown_body(text)):
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


class _HtmlAnchors(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.anchors: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for name, value in attrs:
            if value and (name == "id" or (tag == "a" and name == "name")):
                self.anchors.add(value)


def _heading_text(children: list[Token]) -> str:
    return "".join(
        _heading_text(child.children or []) if child.type == "image" else child.content
        for child in children
        if child.type in {"text", "code_inline", "image", "softbreak", "hardbreak"}
    )


@lru_cache(maxsize=256)
def visible_markdown_anchors(text: str) -> frozenset[str]:
    """GitHub heading IDs and explicit HTML anchors in rendered CommonMark.

    Formatting, punctuation and code blocks do not create heading IDs.
    Unicode letters, marks, numbers and connector punctuation are retained;
    duplicate headings reserve the next unused numbered ID. Custom HTML
    anchors do not affect that numbering.
    """
    tokens = _MARKDOWN.parse(_markdown_body(text))
    headings: set[str] = set()
    html = _HtmlAnchors()
    for index, token in enumerate(tokens):
        if token.type == "heading_open":
            title = _heading_text(tokens[index + 1].children or []).lower()
            base = "".join(
                char
                for char in title
                if char in {" ", "-"}
                or unicodedata.category(char)[0] in {"L", "M", "N"}
                or unicodedata.category(char) == "Pc"
            ).replace(" ", "-")
            anchor, number = base, 0
            while anchor in headings:
                number += 1
                anchor = f"{base}-{number}"
            headings.add(anchor)
        if token.type == "html_block":
            html.feed(token.content)
        for child in token.children or []:
            if child.type == "html_inline":
                html.feed(child.content)
    return frozenset(headings | html.anchors)


def markdown_section_body(text: str, title: str, *, level: int = 2) -> str | None:
    """Find one rendered section; code examples and duplicate titles cannot pass."""
    tokens = _MARKDOWN.parse(_markdown_body(text))
    lines = document_source_lines(text)
    bodies = []
    for index, token in enumerate(tokens):
        if token.type != "heading_open" or token.tag != f"h{level}" or token.map is None:
            continue
        if _heading_text(tokens[index + 1].children or []) != title:
            continue
        start, end = token.map[1], len(lines)
        for following in tokens[index + 1 :]:
            if following.type == "heading_open" and int(following.tag[1:]) <= level and following.map is not None:
                end = following.map[0]
                break
        bodies.append("\n".join(lines[start:end]).strip())
    return bodies[0] if len(bodies) == 1 else None
