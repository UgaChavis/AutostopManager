"""Portable catalog coverage must be independent of one Codex installation."""

from __future__ import annotations

import importlib.util
from pathlib import Path

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
