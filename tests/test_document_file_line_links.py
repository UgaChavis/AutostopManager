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
        write_document(project / name, "\n\n".join([heading, *links]) + "\n")
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
    write_document(project / "docs/agent/references/runbook.md", "# Runbook\n\n[Schema](schema.json:1)\n")
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
    write_document(project / "AGENTS.md", "\n".join(f"[External]({value})" for value in EXTERNAL_DESTINATIONS))
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
    write_document(module.parent / filename, "synthetic file\n")
    module.write_text(module.read_text() + f"\n[Source]({destination})\n")

    report = diagnostics.audit_documentation(navigation_project, check_external_links=False)

    assert report["ok"], report["warnings"]


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
    write_document(project / "docs/agent/references/runbook.md", "# Runbook\n")
    for name in ("A4.md", "A5.md"):
        write_document(project / "docs/agent/modules" / name, "# Initial catalog\n")
    write_document(project / "autostop_manager/__init__.py", '"""Isolated source tree."""\n')
    shutil.copyfile(ROOT / "autostop_manager/document_links.py", project / "autostop_manager/document_links.py")
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
    for arguments in ([], ["--check"]):
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
        assert "docs/agent/references/runbook.md" in json.loads(result.stdout)["project_paths"]
    assert "../../../docs/agent/references/runbook.md" in (project / "docs/agent/modules/A5.md").read_text()
