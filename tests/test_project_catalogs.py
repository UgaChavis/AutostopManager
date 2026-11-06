"""Portable catalog coverage must be independent of one Codex installation."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("project_catalogs", ROOT / "scripts/update-instruction-catalogs.py")
assert SPEC and SPEC.loader
catalogs = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(catalogs)


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def refresh(project, codex):
    a4, a5, _ = catalogs.build_catalogs(project, codex, "2026-10-10")
    write(project / "docs/agent/modules/A4.md", a4)
    write(project / "docs/agent/modules/A5.md", a5)


def test_resolve_skill_uses_current_manifest_and_is_read_only(tmp_path):
    project, codex = tmp_path / "project", tmp_path / "codex"
    project.mkdir()
    codex.mkdir()
    write(codex / "config.toml", '[plugins."visualize@openai-bundled"]\nenabled=true\n')

    def installed(version):
        root = codex / "plugins/cache/openai-bundled/visualize" / version
        write(root / ".codex-plugin/plugin.json", json.dumps({"name": "visualize", "skills": "./skills"}))
        return write(root / "skills/visualize/SKILL.md", "---\nname: visualize\ndescription: synthetic\n---\n")

    old = installed("1.0.66")
    before = {p: p.read_bytes() for p in codex.rglob("*") if p.is_file()}
    assert catalogs.resolve_skill(project, codex, "visualize:visualize") == old
    assert {p: p.read_bytes() for p in codex.rglob("*") if p.is_file()} == before
    new = installed("1.0.74")
    with pytest.raises(ValueError, match="Specify current package version"):
        catalogs.resolve_skill(project, codex, "visualize:visualize")
    shutil.rmtree(old.parents[2])
    assert catalogs.resolve_skill(project, codex, "visualize:visualize") == new
    write(codex / "config.toml", '[plugins."visualize@openai-bundled"]\nenabled=false\n')
    with pytest.raises(ValueError, match="skill_not_selected"):
        catalogs.resolve_skill(project, codex, "visualize:visualize")


def test_resolve_skill_fails_closed_for_absent_or_duplicate_name(tmp_path):
    project, codex = tmp_path / "project", tmp_path / "codex"
    project.mkdir()
    codex.mkdir()
    with pytest.raises(ValueError, match="skill_not_selected"):
        catalogs.resolve_skill(project, codex, "missing")
    write(project / ".agents/skills/one/SKILL.md", "---\nname: same\ndescription: synthetic\n---\n")
    write(codex / "skills/.system/two/SKILL.md", "---\nname: same\ndescription: synthetic\n---\n")
    with pytest.raises(ValueError, match="skill_ambiguous"):
        catalogs.resolve_skill(project, codex, "same")


def test_resolve_project_skill_uses_exact_physical_job_root(tmp_path, monkeypatch, capsys):
    first, second, codex = tmp_path / "first", tmp_path / "second", tmp_path / "codex"
    codex.mkdir()
    for root in (first, second):
        write(
            root / ".agents/skills/manage-autostop-client/SKILL.md",
            "---\nname: manage-autostop-client\ndescription: synthetic\n---\n",
        )
    monkeypatch.chdir(second)
    assert (
        catalogs.resolve_skill(first, codex, "manage-autostop-client")
        == first / ".agents/skills/manage-autostop-client/SKILL.md"
    )
    monkeypatch.setattr(catalogs, "PROJECT", first)
    monkeypatch.setattr(catalogs, "CODEX", codex)
    monkeypatch.setattr("sys.argv", ["update-instruction-catalogs.py", "--resolve-skill", "manage-autostop-client"])
    assert catalogs.main() == 0
    assert json.loads(capsys.readouterr().out)["path"] == str(first / ".agents/skills/manage-autostop-client/SKILL.md")
    alias = tmp_path / "current"
    alias.symlink_to(first, target_is_directory=True)
    with pytest.raises(ValueError, match="project_root_not_physical"):
        catalogs.resolve_skill(alias, codex, "manage-autostop-client")


def test_resolve_rejects_project_skills_redirected_outside_job_root(tmp_path, monkeypatch):
    project, codex = tmp_path / "project", tmp_path / "codex"
    outside = tmp_path / "outside"
    project.mkdir()
    codex.mkdir()
    write(outside / "client/SKILL.md", "---\nname: client\ndescription: synthetic\n---\n")
    (project / ".agents").mkdir()
    (project / ".agents/skills").symlink_to(outside, target_is_directory=True)

    def forbidden(*args, **kwargs):
        raise AssertionError("must not inspect skills outside the pinned job root")

    monkeypatch.setattr(catalogs, "selected_skills", forbidden)
    with pytest.raises(ValueError, match="project_skill_outside_root"):
        catalogs.resolve_skill(project, codex, "client")


def test_resolve_cli_does_not_guess_or_reveal_selection_errors(tmp_path, monkeypatch, capsys):
    project, codex = tmp_path / "project", tmp_path / "codex"
    project.mkdir()
    codex.mkdir()
    monkeypatch.setattr(catalogs, "PROJECT", project)
    monkeypatch.setattr(catalogs, "CODEX", codex)
    monkeypatch.setattr("sys.argv", ["update-instruction-catalogs.py", "--resolve-skill", "missing"])
    assert catalogs.main() == 1
    assert json.loads(capsys.readouterr().out) == {"ok": False, "error": "skill_resolution_failed"}


def test_resolve_cli_does_not_write_project_bytecode(tmp_path):
    project, codex = tmp_path / "project", tmp_path / "codex"
    codex.mkdir()
    source = (ROOT / "scripts/update-instruction-catalogs.py").read_text()
    source = source.replace('CODEX = Path("/root/.codex")', f"CODEX = Path({str(codex)!r})")
    script = write(project / "scripts/update-instruction-catalogs.py", source)
    for name in ("__init__.py", "document_links.py", "instruction_inventory.py", "markdown_links.py"):
        write(project / "autostop_manager" / name, (ROOT / "autostop_manager" / name).read_text())
    expected = write(project / ".agents/skills/client/SKILL.md", "---\nname: client\ndescription: synthetic\n---\n")
    env = os.environ.copy()
    env.pop("PYTHONDONTWRITEBYTECODE", None)
    env["PYTHONPYCACHEPREFIX"] = str(tmp_path / "bytecode")
    result = subprocess.run(
        [sys.executable, str(script), "--resolve-skill", "client"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(result.stdout)["path"] == str(expected)
    assert not (tmp_path / "bytecode" / project.relative_to("/")).exists()


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
@pytest.mark.parametrize(
    "description,expected",
    [
        ('"Synthetic --- metadata"', "Synthetic --- metadata"),
        ("'Quoted: useful --- metadata'", "Quoted: useful --- metadata"),
        (">\n  First --- row\n  second row", "First --- row second row"),
        ("|\n  first\n  ---\n  last", "first --- last"),
        ('"Unicode \\u041c and \\"quotes\\""', 'Unicode М and "quotes"'),
    ],
)
def test_skill_metadata_preserves_yaml_values_and_literal_delimiters(tmp_path, newline, description, expected):
    source = f"---\nname: sample\ndescription: {description}\n---\n\n# Body\n".replace("\n", newline)
    path = write(tmp_path / "SKILL.md", source)
    assert catalogs.metadata(path) == {"name": "sample", "description": expected}


@pytest.mark.parametrize(
    "header",
    [
        "name: sample\ndescription: unquoted: invalid",
        "name: sample\ndescription: [one, two]",
        "name: sample\ndescription: true",
        "name: 123\ndescription: task",
        "name: sample",
        "- name: sample\n- description: task",
        "name: sample\ndescription: !!python/object:object {}",
    ],
)
def test_skill_metadata_rejects_invalid_yaml_and_non_string_fields(tmp_path, header):
    path = write(tmp_path / "SKILL.md", f"---\n{header}\n---\n\n# Body\n")
    with pytest.raises(ValueError, match="skill metadata"):
        catalogs.metadata(path)


@pytest.fixture
def portable_catalogs(tmp_path):
    project, codex = tmp_path / "project", tmp_path / "codex"
    write(project / "AGENTS.md", "# AutoStop\n")
    for name in ("A4.md", "A5.md"):
        write(project / "docs/agent/modules" / name, "# Initial\n")
    for root, name in ((project / ".agents/skills", "project"), (codex / "skills/.system", "core")):
        write(root / name / "SKILL.md", f"---\nname: {name}\ndescription: Task {name}\n---\n\n# {name}\n")
    refresh(project, codex)
    return project, codex


def test_portable_check_is_read_only_and_never_reads_host_selectors(portable_catalogs, monkeypatch):
    project, codex = portable_catalogs
    before = {p: p.read_bytes() for p in project.rglob("*") if p.is_file()}
    write(codex / "config.toml", "synthetic invalid TOML = =")

    def forbidden(*args, **kwargs):
        raise AssertionError("portable catalog check read host installation metadata")

    monkeypatch.setattr(catalogs, "selected_skills", forbidden)
    monkeypatch.setattr(catalogs, "PROJECT", project)
    monkeypatch.setattr(catalogs, "CODEX", codex)
    monkeypatch.setattr("sys.argv", ["update-instruction-catalogs.py", "--check", "--project-only"])
    assert catalogs.main() == 0
    assert catalogs.check_project_catalogs(project)["external_skills_checked"] is False
    assert {p: p.read_bytes() for p in project.rglob("*") if p.is_file()} == before


def test_portable_mode_cannot_overwrite_host_catalogs(portable_catalogs, monkeypatch):
    project, _ = portable_catalogs
    before = (project / "docs/agent/modules/A4.md").read_bytes()
    monkeypatch.setattr(catalogs, "PROJECT", project)
    monkeypatch.setattr("sys.argv", ["update-instruction-catalogs.py", "--project-only"])
    with pytest.raises(SystemExit) as exc:
        catalogs.main()
    assert exc.value.code == 2
    assert (project / "docs/agent/modules/A4.md").read_bytes() == before


@pytest.mark.parametrize("suffix", [".md", ".json"])
def test_portable_check_reports_unlinked_active_documents_before_catalog_refresh(portable_catalogs, suffix):
    project, codex = portable_catalogs
    orphan = write(project / ("docs/agent/references/orphan" + suffix), "{}\n" if suffix == ".json" else "# Orphan\n")
    report = catalogs.check_project_catalogs(project)
    assert report["ok"] is False
    assert report["orphaned_active_documents"] == [orphan.relative_to(project).as_posix()]
    write(project / "AGENTS.md", f"# AutoStop\n\n[Guide]({orphan.relative_to(project).as_posix()})\n")
    report = catalogs.check_project_catalogs(project)
    assert not report["orphaned_active_documents"]
    assert report["mismatches"] == ["A5.md"]
    refresh(project, codex)
    assert catalogs.check_project_catalogs(project)["ok"]


def test_portable_check_detects_changed_project_skill_metadata(portable_catalogs):
    project, _ = portable_catalogs
    path = project / ".agents/skills/project/SKILL.md"
    path.write_text(path.read_text().replace("Task project", "Current project task"))
    assert catalogs.check_project_catalogs(project)["mismatches"] == ["A4.md"]


def test_portable_check_excludes_explicit_history_but_rejects_its_operational_links(portable_catalogs):
    project, _ = portable_catalogs
    write(project / "docs/agent/references/client-instruction-audit.md", "[Historical](missing.md)\n")
    write(project / "docs/agent/drafts/plan.md", "[Draft](missing.md)\n")
    write(project / "docs/reports/review.md", "[Report](missing.md)\n")
    assert catalogs.check_project_catalogs(project)["ok"]
    write(project / "AGENTS.md", "[Old process](docs/agent/references/client-instruction-audit.md)\n")
    with pytest.raises(ValueError, match="Retired instruction is still linked"):
        catalogs.check_project_catalogs(project)


def test_portable_check_rejects_catalog_sections_hidden_as_examples(portable_catalogs):
    project, _ = portable_catalogs
    path = project / "docs/agent/modules/A4.md"
    path.write_text("```markdown\n" + path.read_text() + "```\n")
    assert "A4.md" in catalogs.check_project_catalogs(project)["mismatches"]
