"""Content changes and caller mutations never stale the documentation parser."""

from pathlib import Path

import pytest

from autostop_manager.document_links import LocalDocumentLink, validate_document_reference
from autostop_manager.markdown_links import (
    markdown_section_body,
    visible_markdown_anchors,
    visible_markdown_links,
    visible_markdown_table_row_lines,
)


def test_changed_reference_definition_uses_current_content():
    text = "[catalog][reference]\n\n[reference]: old.md"
    assert visible_markdown_links(text) == ["old.md"]
    assert visible_markdown_links(text.replace("old.md", "new.md")) == ["new.md"]
    assert visible_markdown_links(text) == ["old.md"]


def test_changed_literal_does_not_reuse_a_previously_visible_link():
    assert visible_markdown_links("[catalog](E4.md)") == ["E4.md"]
    assert visible_markdown_links("`[catalog](E4.md)`") == []


def test_caller_cannot_change_subsequent_link_results():
    text = "[first](E4.md) and [second](E5.md)"
    links = visible_markdown_links(text)
    links.clear()
    assert visible_markdown_links(text) == ["E4.md", "E5.md"]


def test_wrapping_a_table_as_a_literal_does_not_reuse_visible_rows():
    table = "| Module |\n| --- |\n| [E4](E4.md) |\n"
    assert visible_markdown_table_row_lines(table) == frozenset({0, 2})
    assert visible_markdown_table_row_lines("```markdown\n" + table + "```\n\n[E4](E4.md)") == frozenset()
    assert visible_markdown_table_row_lines("<!--\n" + table + "-->\n\n[E4](E4.md)") == frozenset()
    assert visible_markdown_table_row_lines(table) == frozenset({0, 2})


def test_unicode_heading_ids_follow_rendered_text_and_collision_numbering():
    text = (
        "# Раздел **повтор**\n\n# Раздел повтор\n\n# Раздел повтор-1\n\n# Раздел повтор\n\n"
        "## [Été](https://example.invalid) `OEM` &amp; Θ!\n\n"
        "Setext Heading\n---\n\n# Cafe\u0301 _name_\n"
    )
    assert visible_markdown_anchors(text) == {
        "раздел-повтор",
        "раздел-повтор-1",
        "раздел-повтор-1-1",
        "раздел-повтор-2",
        "été-oem--θ",
        "setext-heading",
        "cafe\u0301-name",
    }


def test_custom_anchors_are_visible_but_code_and_comments_are_not():
    text = (
        '<a name="Custom-Anchor"></a>\n\n<div id="details"></div>\n\n'
        '<a name="same"></a>\n\n# Same\n\n# Same\n\n'
        '```html\n<a name="fake-html"></a>\n# Fake heading\n```\n\n'
        '`<a name="fake-inline"></a>`\n\n<!-- <a name="fake-comment"></a> -->\n'
    )
    assert visible_markdown_anchors(text) == {"Custom-Anchor", "details", "same", "same-1"}


def test_section_selection_requires_one_visible_heading():
    text = "```markdown\n## AutoStop Manager\nfake\n```\n\n## AutoStop Manager\nreal\n\n## External\nother\n"
    assert markdown_section_body(text, "AutoStop Manager") == "real"
    assert markdown_section_body(text + "\n## AutoStop Manager\nduplicate\n", "AutoStop Manager") is None


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
@pytest.mark.parametrize("separator", ["\u2028", "\u2029", "\x85"])
def test_section_offsets_match_commonmark_lines_with_literal_unicode_separators(newline, separator):
    text = newline.join(
        [
            f"Introduction{separator}literal",
            "",
            "## AutoStop Manager",
            f"body{separator}literal",
            "",
            "## Other",
            "other",
            "",
        ]
    )
    assert markdown_section_body(text, "AutoStop Manager") == f"body{separator}literal"


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
@pytest.mark.parametrize("marker", ["---", "---  "])
def test_skill_frontmatter_is_metadata_with_original_body_line_numbers(newline, marker):
    text = newline.join(
        [
            "---",
            "name: demo",
            'description: "[obsolete](missing.md)"',
            marker,
            "",
            "## AutoStop Manager",
            "[body](body.md:1#body)",
            "",
            "| Document |",
            "| --- |",
            "| [Body](body.md) |",
            "",
        ]
    )

    assert visible_markdown_links(text) == ["body.md:1#body", "body.md"]
    assert visible_markdown_anchors(text) == {"autostop-manager"}
    assert visible_markdown_table_row_lines(text) == {8, 10}
    assert markdown_section_body(text, "AutoStop Manager") == (
        "[body](body.md:1#body)\n\n| Document |\n| --- |\n| [Body](body.md) |"
    )
    validate_document_reference(LocalDocumentLink("skill.md", fragment="autostop-manager"), Path("skill.md"), text)
    with pytest.raises(ValueError, match="document_link_anchor_missing"):
        validate_document_reference(
            LocalDocumentLink("skill.md", fragment="name-demodescription-obsolete"), Path("skill.md"), text
        )


def test_multiline_frontmatter_cannot_add_sections_tables_or_reference_definitions():
    text = (
        "---\nname: demo\ndescription: |\n  ## AutoStop Manager\n"
        "  | Metadata |\n  | --- |\n  | [obsolete](missing.md) |\n"
        "---\n\n## AutoStop Manager\n[body](body.md)\n\n"
        "| Document |\n| --- |\n| [Body](body.md) |\n"
    )
    assert visible_markdown_links(text) == ["body.md", "body.md"]
    assert visible_markdown_anchors(text) == {"autostop-manager"}
    assert visible_markdown_table_row_lines(text) == {12, 14}
    assert markdown_section_body(text, "AutoStop Manager") == (
        "[body](body.md)\n\n| Document |\n| --- |\n| [Body](body.md) |"
    )

    references = "---\nname: demo\n[hidden]: missing.md\n---\n\n[hidden]\n[real][entry]\n\n[entry]: body.md\n"
    assert visible_markdown_links(references) == ["body.md"]


@pytest.mark.parametrize(
    "text,anchors,links",
    [
        ("---\n\n# Body\n[body](body.md)\n", {"body"}, ["body.md"]),
        ("Introduction\n\n---\n# Body\n[body](body.md)\n", {"body"}, ["body.md"]),
        ("Setext heading\n---\n\n[body](body.md)\n", {"setext-heading"}, ["body.md"]),
        ("```yaml\n---\n[metadata](missing.md)\n---\n```\n# Body\n", {"body"}, []),
    ],
)
def test_only_closed_leading_frontmatter_is_metadata(text, anchors, links):
    assert visible_markdown_anchors(text) == anchors
    assert visible_markdown_links(text) == links
