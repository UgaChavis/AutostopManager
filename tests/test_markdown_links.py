"""Content changes and caller mutations never stale the documentation parser."""

from autostop_manager.markdown_links import visible_markdown_links, visible_markdown_table_row_lines


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
