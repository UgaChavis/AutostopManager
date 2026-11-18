"""Portable catalog coverage must be independent of one Codex installation."""

from __future__ import annotations

import importlib.util
import json
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


def refresh(project):
    a4, a5, _ = catalogs.build_catalogs(project, day="2026-10-10")
    write(project / "docs/agent/modules/A4.md", a4)
    write(project / "docs/agent/modules/A5.md", a5)


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
    refresh(project)
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


def test_project_only_flag_requires_check_and_writes_nothing(portable_catalogs, monkeypatch):
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
    project, _ = portable_catalogs
    orphan = write(project / ("docs/agent/references/orphan" + suffix), "{}\n" if suffix == ".json" else "# Orphan\n")
    report = catalogs.check_project_catalogs(project)
    assert report["ok"] is False
    assert report["orphaned_active_documents"] == [orphan.relative_to(project).as_posix()]
    write(project / "AGENTS.md", f"# AutoStop\n\n[Guide]({orphan.relative_to(project).as_posix()})\n")
    report = catalogs.check_project_catalogs(project)
    assert not report["orphaned_active_documents"]
    assert report["mismatches"] == ["A5.md"]
    refresh(project)
    assert catalogs.check_project_catalogs(project)["ok"]


def test_portable_check_detects_changed_project_skill_metadata(portable_catalogs):
    project, _ = portable_catalogs
    path = project / ".agents/skills/project/SKILL.md"
    path.write_text(path.read_text().replace("Task project", "Current project task"))
    assert catalogs.check_project_catalogs(project)["mismatches"] == ["A4.md"]


def test_full_check_follows_host_skills_without_catalog_drift(portable_catalogs, monkeypatch, capsys):
    project, codex = portable_catalogs
    catalog_paths = [project / "docs/agent/modules" / name for name in ("A4.md", "A5.md")]
    original = [path.read_bytes() for path in catalog_paths]
    monkeypatch.setattr(catalogs, "PROJECT", project)
    monkeypatch.setattr(catalogs, "CODEX", codex)

    def run_catalogs(*args, expected_exit=0):
        monkeypatch.setattr("sys.argv", ["update-instruction-catalogs.py", *args])
        assert catalogs.main() == expected_exit
        return json.loads(capsys.readouterr().out)

    assert run_catalogs("--check")["codex_documents"] == 1

    extra = write(
        codex / "skills/extra/SKILL.md",
        "---\nname: extra\ndescription: New host skill\n---\n\n# Extra\n",
    )
    assert run_catalogs("--check")["codex_documents"] == 2
    extra.write_text(extra.read_text().replace("name: extra", "name: renamed-extra"), encoding="utf-8")
    (codex / "skills/.system/core/SKILL.md").unlink()

    report = run_catalogs("--check")
    assert report["ok"] is True and report["external_skills_checked"] is True
    assert report["mismatches"] == [] and report["codex_documents"] == 1
    inventory = run_catalogs("--list-external-skills")
    assert [row["name"] for row in inventory["skills"]] == ["renamed-extra"]
    assert run_catalogs("--list-external-skills", "renamed-extra")["skill"]["path"] == str(extra)
    assert [path.read_bytes() for path in catalog_paths] == original

    stale = original[0] + "\nУстаревшая строка\n".encode()
    catalog_paths[0].write_bytes(stale)
    assert run_catalogs("--check", expected_exit=1)["mismatches"] == ["A4.md"]
    assert catalog_paths[0].read_bytes() == stale


def test_full_check_reports_ambiguous_host_skill_without_project_drift(portable_catalogs, monkeypatch, capsys):
    project, codex = portable_catalogs
    write(
        codex / "skills/alias/SKILL.md",
        "---\nname: core\ndescription: Host alias\n---\n\n# Alias\n",
    )
    monkeypatch.setattr(catalogs, "PROJECT", project)
    monkeypatch.setattr(catalogs, "CODEX", codex)
    monkeypatch.setattr("sys.argv", ["update-instruction-catalogs.py", "--check"])

    assert catalogs.main() == 1

    report = json.loads(capsys.readouterr().out)
    assert report["ok"] is False
    assert report["mismatches"] == []
    assert report["ambiguous_external_skills"] == ["core"]


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


@pytest.mark.parametrize("name", ["A4.md", "A5.md"])
def test_portable_check_rejects_missing_entry_without_numeric_header(portable_catalogs, name):
    project, _ = portable_catalogs
    path = project / "docs/agent/modules" / name
    rows = path.read_text().splitlines(keepends=True)
    entry = next(row for row in rows if row.startswith("| ["))
    path.write_text("".join(row for row in rows if row != entry))

    report = catalogs.check_project_catalogs(project)

    assert report["ok"] is False
    assert report["mismatches"] == [name]
