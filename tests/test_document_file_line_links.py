from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from autostop_manager import diagnostics
from autostop_manager.document_links import (
    LocalDocumentLink,
    local_document_link_target,
    parse_local_document_link,
    validate_document_reference,
)

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("file_line_catalogs", ROOT / "scripts/update-instruction-catalogs.py")
assert SPEC and SPEC.loader
catalogs = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(catalogs)


def write_document(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def navigation_project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    for module_id, name in diagnostics.MODULE_DOCUMENTS.items():
        heading = "# AutoStop" if module_id == "A2" else f"# {module_id}"
        children = [child for child, parent in diagnostics.MODULE_PARENTS.items() if parent == module_id]
        if module_id == "A1":
            children.extend(
                child for child, parent in diagnostics.MODULE_PARENTS.items() if parent is None and child != "A1"
            )
        links = [f"[{child}](../../../AGENTS.md)" if child == "A2" else f"[{child}]({child}.md)" for child in children]
        if module_id == "A2":
            links.append("[A1](docs/agent/modules/A1.md)")
        write_document(project / name, "\n\n".join([heading, *links]) + "\n" * 12 + "## Section\n")
    for name in diagnostics.SKILL_DOCUMENTS:
        skill_name = Path(name).parent.name
        write_document(project / name, f"---\nname: {skill_name}\ndescription: Synthetic skill\n---\n\n# Skill\n")
    assert diagnostics.audit_documentation(project, check_external_links=False)["ok"]
    return project


@pytest.mark.parametrize(
    "target",
    [
        "E10.md:12",
        "./E10.md:12",
        "E10.md:01",
        "./E10.md:00012",
        "E10.md:12#section",
        "E10.md:12?version=2#section",
        "E10.md%3A12",
        "E10%2Emd:12",
        "E10.md:%31%32",
    ],
)
def test_public_audit_accepts_module_navigation_with_file_line(navigation_project: Path, target: str) -> None:
    index = navigation_project / diagnostics.MODULE_DOCUMENTS["E1"]
    index.write_text(index.read_text().replace("[E10](E10.md)", f"[E10]({target})"))

    report = diagnostics.audit_documentation(navigation_project, check_external_links=False)

    assert report["ok"], report["warnings"]


@pytest.mark.parametrize("target", ["missing.md:12", "./missing.md:12"])
def test_public_audit_reports_missing_file_line_destination(navigation_project: Path, target: str) -> None:
    module_name = diagnostics.MODULE_DOCUMENTS["E10"]
    module = navigation_project / module_name
    module.write_text(module.read_text() + f"\n[Missing]({target})\n")

    report = diagnostics.audit_documentation(navigation_project, check_external_links=False)

    assert report["ok"] is False
    assert f"document_link_invalid:{module_name}" in report["warnings"]


def test_generated_catalog_follows_unseeded_file_line_references(tmp_path: Path) -> None:
    project = tmp_path / "project"
    write_document(project / "AGENTS.md", "[Index](docs/agent/references/index.md:3)\n")
    write_document(project / "docs/agent/references/index.md", "# Index\n\n[Runbook](runbook.md:12)\n")
    write_document(project / "docs/agent/references/runbook.md", "# Runbook\n\n[Schema](schema.json:1)\n" + "\n" * 9)
    write_document(project / "docs/agent/references/schema.json", '{"synthetic": true}\n')

    _, instructions, summary = catalogs.build_catalogs(project, tmp_path / "empty-codex", "2026-10-05")

    assert summary["project_paths"] == [
        "AGENTS.md",
        "docs/agent/references/index.md",
        "docs/agent/references/runbook.md",
        "docs/agent/references/schema.json",
    ]
    assert "../../../docs/agent/references/runbook.md" in instructions
    assert "../../../docs/agent/references/schema.json" in instructions


def test_generated_catalog_reports_missing_unseeded_file_line_reference(tmp_path: Path) -> None:
    project = tmp_path / "project"
    write_document(project / "AGENTS.md", "[Index](docs/agent/references/index.md)\n")
    write_document(project / "docs/agent/references/index.md", "# Index\n\n[Missing](missing.md:12)\n")

    with pytest.raises(ValueError, match="Missing or outside-project instruction"):
        catalogs.build_catalogs(project, tmp_path / "empty-codex", "2026-10-05")


@pytest.mark.parametrize("prefix", ["", "./"])
@pytest.mark.parametrize("line", ["0", "00", "-1", "+1", "١٢", "%30"])
def test_public_audit_rejects_invalid_numeric_file_line(navigation_project: Path, prefix: str, line: str) -> None:
    index_name = diagnostics.MODULE_DOCUMENTS["E1"]
    index = navigation_project / index_name
    index.write_text(index.read_text().replace("[E10](E10.md)", f"[E10]({prefix}E10.md:{line})"))

    report = diagnostics.audit_documentation(navigation_project, check_external_links=False)

    assert report["ok"] is False
    assert f"document_link_invalid:{index_name}" in report["warnings"]
    assert f"module_link_missing:{index_name}:{diagnostics.MODULE_DOCUMENTS['E10']}" in report["warnings"]


@pytest.mark.parametrize("prefix", ["", "./"])
@pytest.mark.parametrize("line", ["0", "00", "-1", "+1", "١٢", "%30"])
def test_generated_catalog_rejects_invalid_numeric_reference(tmp_path: Path, prefix: str, line: str) -> None:
    project = tmp_path / "project"
    write_document(project / "AGENTS.md", "[Index](docs/agent/references/index.md)\n")
    write_document(project / "docs/agent/references/index.md", f"# Index\n\n[Runbook]({prefix}runbook.md:{line})\n")
    write_document(project / "docs/agent/references/runbook.md", "# Runbook\n")

    with pytest.raises(ValueError, match="document_link_line_invalid"):
        catalogs.build_catalogs(project, tmp_path / "empty-codex", "2026-10-05")


@pytest.mark.parametrize("suffix", [".json", ".py", ".toml", ".txt", ".sh", ".PY"])
def test_public_audit_resolves_app_source_file_basename_lines(navigation_project: Path, suffix: str) -> None:
    module = navigation_project / diagnostics.MODULE_DOCUMENTS["E10"]
    write_document(module.parent / f"example{suffix}", "synthetic file\n")
    module.write_text(module.read_text() + f"\n[Source](example{suffix}:01)\n")

    report = diagnostics.audit_documentation(navigation_project, check_external_links=False)

    assert report["ok"], report["warnings"]


EXTERNAL_DESTINATIONS = [
    "https://example.invalid:443/runbook.md:12",
    "http://example.invalid:8080/runbook.md:12?port=22#section",
    "mailto:user@example.invalid",
    "codex://review?pr=https%3A%2F%2Fexample.invalid&line=12",
    "file:///etc/runbook.md:12",
    "custom:12",
    "custom.scheme:12",
    "custom+v1:12",
    "C:/runbook.md:12",
    "//example.invalid:443/runbook.md:12",
    "#section",
    "?version=12",
]


@pytest.mark.parametrize("destination", EXTERNAL_DESTINATIONS)
def test_public_audit_preserves_external_uri_and_authority_links(navigation_project: Path, destination: str) -> None:
    module = navigation_project / diagnostics.MODULE_DOCUMENTS["E10"]
    module.write_text(module.read_text() + f"\n[External]({destination})\n")

    report = diagnostics.audit_documentation(navigation_project, check_external_links=False)

    assert report["ok"], report["warnings"]


def test_generated_catalog_does_not_follow_external_uris_or_authorities(tmp_path: Path) -> None:
    project = tmp_path / "project"
    write_document(
        project / "AGENTS.md", "# Section\n\n" + "\n".join(f"[External]({value})" for value in EXTERNAL_DESTINATIONS)
    )
    # These files must not turn URI/authority links into local navigation.
    write_document(project / "custom.scheme", "synthetic collision\n")
    write_document(project / "runbook.md", "# Authority path collision\n")

    _, instructions, summary = catalogs.build_catalogs(project, tmp_path / "empty-codex", "2026-10-05")

    assert summary["project_paths"] == ["AGENTS.md"]
    assert "../../../runbook.md" not in instructions


@pytest.mark.parametrize(
    ("filename", "destination"),
    [
        ("example.json", "example.json%3A12"),
        ("example.json", "example.json:12?ignored=elsewhere.json:0#section"),
        ("example#section.json", "example%23section.json:12"),
        ("example?version.json", "example%3Fversion.json:12"),
        ("example.json%3A12", "example.json%253A12"),
        ("example.json:abc", "./example.json:abc"),
    ],
)
def test_public_audit_decodes_paths_once_after_uri_fields(
    navigation_project: Path, filename: str, destination: str
) -> None:
    module = navigation_project / diagnostics.MODULE_DOCUMENTS["E10"]
    write_document(module.parent / filename, "synthetic file\n" * 12)
    module.write_text(module.read_text() + f"\n[Source]({destination})\n")

    report = diagnostics.audit_documentation(navigation_project, check_external_links=False)

    assert report["ok"], report["warnings"]


@pytest.mark.parametrize("destination", ["#раздел-повтор-1", "guide.md#раздел-повтор-1", "guide.md:3#раздел-повтор-1"])
def test_audit_and_catalog_validate_unicode_duplicate_heading_destinations(navigation_project: Path, destination: str):
    module = navigation_project / diagnostics.MODULE_DOCUMENTS["E10"]
    content = "# Раздел повтор\n\n## Раздел **повтор**\n"
    write_document(module.parent / "guide.md", content)
    module.write_text(module.read_text() + "\n" + content + f"\n[Section]({destination})\n")
    assert diagnostics.audit_documentation(navigation_project, check_external_links=False)["ok"]
    assert catalogs.build_catalogs(navigation_project, navigation_project / "missing-codex")[2]["project_paths"]


@pytest.mark.parametrize("destination", ["#missing", "guide.md#missing", "guide.md:1#missing", "guide.md#fake-heading"])
def test_audit_and_catalog_reject_missing_or_literal_heading_destinations(navigation_project: Path, destination: str):
    module_name = diagnostics.MODULE_DOCUMENTS["E10"]
    module = navigation_project / module_name
    write_document(module.parent / "guide.md", "# Real heading\n\n```markdown\n# Fake heading\n```\n")
    module.write_text(module.read_text() + f"\n[Broken]({destination})\n")
    report = diagnostics.audit_documentation(navigation_project, check_external_links=False)
    assert not report["ok"]
    assert f"document_link_invalid:{module_name}" in report["warnings"]
    with pytest.raises(ValueError, match="document_link_anchor_missing"):
        catalogs.build_catalogs(navigation_project, navigation_project / "missing-codex")


@pytest.mark.parametrize("suffix", [".md", ".py", ".json"])
@pytest.mark.parametrize("content", ["first\nsecond", "first\r\nsecond\r\n"])
def test_line_references_accept_the_last_line_and_reject_past_eof(navigation_project: Path, suffix: str, content: str):
    module_name = diagnostics.MODULE_DOCUMENTS["E10"]
    module = navigation_project / module_name
    write_document(module.parent / f"boundary{suffix}", content)
    original = module.read_text()
    module.write_text(original + f"\n[Last](boundary{suffix}:2)\n")
    assert diagnostics.audit_documentation(navigation_project, check_external_links=False)["ok"]
    module.write_text(original + f"\n[Past EOF](boundary{suffix}:3)\n")
    report = diagnostics.audit_documentation(navigation_project, check_external_links=False)
    assert not report["ok"]
    assert f"document_link_invalid:{module_name}" in report["warnings"]
    with pytest.raises(ValueError, match="document_link_line_out_of_range"):
        catalogs.build_catalogs(navigation_project, navigation_project / "missing-codex")


@pytest.mark.parametrize("separator", ["\u2028", "\u2029", "\x85", "\v", "\f", "\x1c", "\x1d", "\x1e"])
def test_unicode_and_control_separators_do_not_create_source_line_destinations(separator):
    text = f"first{separator}second\n"
    validate_document_reference(LocalDocumentLink("source.py", line=1), Path("source.py"), text)
    with pytest.raises(ValueError, match="document_link_line_out_of_range"):
        validate_document_reference(LocalDocumentLink("source.py", line=2), Path("source.py"), text)


@pytest.mark.parametrize(
    "text,last_line",
    [("", 0), ("first", 1), ("first\n", 1), ("first\r", 1), ("first\r\n", 1), ("\n", 1), ("first\n\n", 2)],
)
def test_terminal_newline_does_not_create_an_extra_source_line(text, last_line):
    if last_line:
        validate_document_reference(LocalDocumentLink("source.py", line=last_line), Path("source.py"), text)
    with pytest.raises(ValueError, match="document_link_line_out_of_range"):
        validate_document_reference(LocalDocumentLink("source.py", line=last_line + 1), Path("source.py"), text)


def test_fragment_decoding_preserves_custom_anchor_case_and_path_only_compatibility(navigation_project: Path):
    module = navigation_project / diagnostics.MODULE_DOCUMENTS["E10"]
    write_document(module.parent / "guide.md", '<a name="Точное-Имя"></a>\n')
    module.write_text(
        module.read_text() + "\n[Custom](guide.md#%D0%A2%D0%BE%D1%87%D0%BD%D0%BE%D0%B5-%D0%98%D0%BC%D1%8F)\n"
    )
    assert diagnostics.audit_documentation(navigation_project, check_external_links=False)["ok"]
    assert local_document_link_target("guide.md:001#anchor") == "guide.md"
    assert local_document_link_target("#anchor") is None
    reference = parse_local_document_link("guide.md:001#encoded%2520anchor")
    assert reference and (reference.line, reference.fragment) == (1, "encoded%20anchor")


@pytest.mark.parametrize("check_external_links", [False, True])
@pytest.mark.parametrize("escape", ["relative", "encoded", "symlink"])
def test_public_audit_keeps_file_line_containment_and_symlink_guards(
    navigation_project: Path, check_external_links: bool, escape: str
) -> None:
    module_name = diagnostics.MODULE_DOCUMENTS["E10"]
    module = navigation_project / module_name
    outside = write_document(navigation_project.parent / "outside.py", "synthetic outside file\n")
    if escape == "symlink":
        (module.parent / "alias.py").symlink_to(outside)
        destination = "alias.py:12"
    elif escape == "encoded":
        destination = "..%2F..%2F..%2F..%2Foutside.py%3A12"
    else:
        destination = "../../../../outside.py:12"
    module.write_text(module.read_text() + f"\n[Outside]({destination})\n")

    report = diagnostics.audit_documentation(navigation_project, check_external_links=check_external_links)

    assert report["ok"] is False
    assert f"document_link_invalid:{module_name}" in report["warnings"]


@pytest.mark.parametrize("check_external_links", [False, True])
@pytest.mark.parametrize("suffix", ["md", "py:12"])
def test_audit_resolves_symlink_before_parent_traversal(
    navigation_project: Path, check_external_links: bool, suffix: str
) -> None:
    outside = navigation_project.parent / "outside"
    (outside / "child").mkdir(parents=True)
    (navigation_project / "docs/jump").symlink_to(outside / "child", target_is_directory=True)
    filename = "target." + suffix.split(":")[0]
    write_document(outside / filename, "synthetic outside target\n")
    write_document(navigation_project / "docs" / filename, "synthetic inside shadow\n")
    module_name = diagnostics.MODULE_DOCUMENTS["D1"]
    module = navigation_project / module_name
    module.write_text(module.read_text() + f"\n[Target](../../jump/../target.{suffix})\n")

    report = diagnostics.audit_documentation(navigation_project, check_external_links=check_external_links)

    assert report["ok"] is False
    assert f"document_link_invalid:{module_name}" in report["warnings"]


def test_audit_does_not_read_required_module_aliased_to_retired_content(navigation_project, monkeypatch):
    module = navigation_project / diagnostics.MODULE_DOCUMENTS["A3"]
    target = write_document(navigation_project / "docs/drafts/old.md", "# A3\n")
    module.unlink()
    module.symlink_to(target)
    original = diagnostics.read_instruction_bytes

    def guarded(path):
        assert path != target, "Retired content must not be read by the audit fallback"
        return original(path)

    monkeypatch.setattr(diagnostics, "read_instruction_bytes", guarded)

    report = diagnostics.audit_documentation(navigation_project, check_external_links=False)

    assert report["ok"] is False
    assert "instruction_inventory_mismatch" in report["warnings"]


@pytest.mark.parametrize("check_external_links", [False, True])
def test_public_audit_keeps_exact_m2_journal_allowlist(
    navigation_project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, check_external_links: bool
) -> None:
    journal_root = tmp_path / "private-journal"
    entry = write_document(journal_root / "current-state.md", "# Synthetic technical state\n")
    monkeypatch.setattr(diagnostics, "ROLE_JOURNAL_ROOT", journal_root)
    monkeypatch.setattr(diagnostics, "ROLE_JOURNAL_ENTRIES", {entry})
    module_name = diagnostics.MODULE_DOCUMENTS["M2"]
    module = navigation_project / module_name
    module.write_text(module.read_text() + f"\n[State]({entry}:01)\n")

    assert diagnostics.audit_documentation(navigation_project, check_external_links=check_external_links)["ok"]

    forbidden = write_document(journal_root / "other.md", "# Unapproved entry\n")
    module.write_text(module.read_text() + f"\n[Other]({forbidden}:12)\n")
    report = diagnostics.audit_documentation(navigation_project, check_external_links=check_external_links)
    assert f"document_link_invalid:{module_name}" in report["warnings"]


def test_direct_catalog_cli_uses_helper_from_its_source_tree(tmp_path: Path) -> None:
    project = tmp_path / "isolated-project"
    write_document(project / "AGENTS.md", "[Index](docs/agent/references/index.md)\n")
    write_document(project / "docs/agent/references/index.md", "# Index\n\n[Runbook](runbook.md:12)\n")
    write_document(project / "docs/agent/references/runbook.md", "# Runbook\n" * 12)
    for name in ("A4.md", "A5.md"):
        write_document(project / "docs/agent/modules" / name, "# Initial catalog\n")
    write_document(project / "autostop_manager/__init__.py", '"""Isolated source tree."""\n')
    shutil.copyfile(ROOT / "autostop_manager/document_links.py", project / "autostop_manager/document_links.py")
    shutil.copyfile(
        ROOT / "autostop_manager/instruction_inventory.py", project / "autostop_manager/instruction_inventory.py"
    )
    markdown_helper = ROOT / "autostop_manager/markdown_links.py"
    if markdown_helper.is_file():
        shutil.copyfile(markdown_helper, project / "autostop_manager/markdown_links.py")
    script = project / "scripts/update-instruction-catalogs.py"
    script.parent.mkdir(parents=True)
    script_source = (ROOT / "scripts/update-instruction-catalogs.py").read_text()
    # Only the external-skill inventory location is replaced in this CLI fixture.
    script.write_text(
        script_source.replace('CODEX = Path("/root/.codex")', f"CODEX = Path({str(tmp_path / 'empty-codex')!r})")
    )
    shadow = tmp_path / "editable-owner-shadow"
    write_document(shadow / "autostop_manager/__init__.py", '"""Should never be imported."""\n')
    write_document(shadow / "autostop_manager/document_links.py", "raise AssertionError('wrong helper source')\n")
    env = {**os.environ, "PYTHONPATH": str(shadow), "PYTHONDONTWRITEBYTECODE": "1"}
    for arguments in ([], ["--check"], ["--check", "--project-only"]):
        result = subprocess.run(
            [sys.executable, "-B", str(script), *arguments],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        report = json.loads(result.stdout)
        if "--project-only" in arguments:
            assert report["project_documents"] == 5 and report["orphaned_active_document_count"] == 0
        else:
            assert "docs/agent/references/runbook.md" in report["project_paths"]
    assert "../../../docs/agent/references/runbook.md" in (project / "docs/agent/modules/A5.md").read_text()
