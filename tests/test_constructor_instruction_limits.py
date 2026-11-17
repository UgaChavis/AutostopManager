"""The source gate must honor the current constructor's whole-module contract."""

from __future__ import annotations

from pathlib import Path
import shutil

import pytest

from autostop_manager import diagnostics
from autostop_manager.document_links import parse_local_document_link
from autostop_manager.markdown_links import visible_markdown_links


@pytest.fixture
def project(tmp_path):
    root = Path(__file__).resolve().parents[1]
    target = tmp_path / "project"
    target.mkdir()
    shutil.copy2(root / "AGENTS.md", target / "AGENTS.md")
    shutil.copytree(root / "docs", target / "docs")
    shutil.copytree(root / ".agents", target / ".agents")
    # Keep referenced code inert: this fixture audits instruction text only.
    for document in target.rglob("*.md"):
        for link in visible_markdown_links(document.read_text()):
            reference = parse_local_document_link(link)
            if reference is None or not reference.path:
                continue
            path = (document.parent / reference.path).resolve()
            if path.is_relative_to(target) and not path.exists() and path.suffix not in {".md", ".json"}:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("\n" * (reference.line or 1))
    return target


@pytest.mark.parametrize(
    "module_id,length,too_long",
    [("A4", 30_000, False), ("A4", 30_001, True), ("B1", 30_001, True), ("A2", 30_001, True)],
)
def test_constructor_limit_covers_catalog_and_other_module_text(project, module_id, length, too_long):
    name = diagnostics.MODULE_DOCUMENTS[module_id]
    path = project / name
    text = path.read_text()
    assert len(text) < length
    path.write_text(text + "x" * (length - len(text)))

    result = diagnostics.audit_documentation(project, check_external_links=False)

    warning = f"constructor_instruction_too_long:{name}"
    assert (warning in result["warnings"]) is too_long
    if too_long:
        assert result["ok"] is False
    else:
        assert result["ok"] is True


def test_long_a5_keeps_the_existing_constructor_pointer_exception(project):
    path = project / diagnostics.MODULE_DOCUMENTS["A5"]
    path.write_text(path.read_text() + "x" * 30_001)

    result = diagnostics.audit_documentation(project, check_external_links=False)

    assert result["ok"] is True
    assert not any(warning.startswith("constructor_instruction_too_long:") for warning in result["warnings"])
