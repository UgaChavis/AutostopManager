"""Content changes and caller mutations never stale the documentation parser."""

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
