from __future__ import annotations

from datetime import datetime, UTC
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest

from autostop_manager import diagnostics

ROOT = Path(__file__).resolve().parents[1]


def load_script(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), ROOT / "scripts" / (name + ".py"))
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


catalogs = load_script("update-instruction-catalogs")
journal = load_script("m2-journal")


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def skill(path, name):
    return write(path, f"---\nname: {name}\ndescription: Task {name}\n---\n\n# {name}\n")


def installed_package(codex, package, *, version="1", marketplace="openai-curated-remote", skills="./skills/"):
    root = codex / "plugins/cache" / marketplace / package / version
    info = {"name": package, "version": version}
    if skills is not None:
        info["skills"] = skills
    write(root / ".codex-plugin/plugin.json", json.dumps(info))
    if marketplace == "openai-curated-remote":
        write(
            root.parent / ".codex-remote-plugin-install.json",
            json.dumps({"schema_version": 1, "remote_plugin_id": "synthetic-" + package}),
        )
    if isinstance(skills, str):
        (root / skills).mkdir(parents=True, exist_ok=True)
    return root


def test_catalogs_do_not_import_or_recursively_index_retired_packages(tmp_path):
    project, codex = tmp_path / "project", tmp_path / "codex"
    write(project / "AGENTS.md", "[A1](docs/agent/modules/A1.md)")
    write(project / "docs/agent/modules/A1.md", "[M1](M1.md) [M2](M2.md) [ref](../references/live.md)")
    write(project / "docs/agent/modules/M1.md", "# M1")
    write(project / "docs/agent/modules/M2.md", "# M2")
    write(project / "docs/agent/modules/A4.md", "[old](/root/.codex/plugins/cache/sales/1/skills/index/SKILL.md)")
    write(project / "docs/agent/modules/A5.md", "# old inventory")
    write(project / "docs/agent/references/live.md", "# live")
    retired = write(project / "docs/agent/old-roadmap.md", "# old")
    skill(project / ".agents/skills/project/SKILL.md", "project")
    skill(codex / "skills/.system/core/SKILL.md", "core")
    skill(codex / "skills/.system/review-agent/SKILL.md", "review-agent")
    cache = codex / "plugins/cache/openai-curated-remote"
    for package in catalogs.PACKAGES:
        marketplace = "openai-bundled" if package == "visualize" else "openai-curated-remote"
        root = installed_package(codex, package, marketplace=marketplace)
        folder = root / "skills/index"
        skill(folder / "SKILL.md", "index")
        write(folder / "references/old.md", "# should not be indexed")
        skill(folder / "nested/SKILL.md", "nested")
    for package in ("sales", "pages", "work-pets", "openai-templates", "figma"):
        skill(cache / package / "1/skills/index/SKILL.md", "index")
    write(
        codex / "config.toml",
        '[plugins."github@openai-curated"]\nenabled=false\n[plugins."visualize@openai-bundled"]\nenabled=true\n',
    )
    before = (project / "docs/agent/modules/A5.md").read_text()

    a4, a5, counts = catalogs.build_catalogs(project, codex, "2026-10-01")

    assert len(counts["project_paths"]) == 8
    assert counts["skills"] == 9  # project + system + seven enabled selected packages
    assert counts["documents"] == 16
    assert counts["codex_documents"] == 8
    assert "M1.md" in a5 and "M2.md" in a5
    assert retired.relative_to(project).as_posix() not in a5
    assert (project / "docs/agent/modules/A5.md").read_text() == before
    assert all(
        name not in a4 + a5
        for name in (
            "sales/",
            "pages/",
            "work-pets/",
            "openai-templates/",
            "figma/",
            "nested/",
            "references/old.md",
            "github:index",
        )
    )


def test_catalogs_refuse_linked_retired_or_missing_instructions(tmp_path):
    write(tmp_path / "AGENTS.md", "[missing](docs/old.md)")
    with pytest.raises(ValueError, match="Missing"):
        catalogs.project_documents(tmp_path)
    write(tmp_path / "docs/old.md", "[old](archive/old.md)")
    write(tmp_path / "docs/archive/old.md", "# history")
    with pytest.raises(ValueError, match="Retired"):
        catalogs.project_documents(tmp_path)


@pytest.mark.parametrize(
    "example",
    [
        "[unused]: docs/missing.md",
        "```markdown\n[Missing](docs/missing.md)\n```",
        "~~~markdown\n[Missing](docs/missing.md)\n~~~",
        "`[Missing](docs/missing.md)`",
        "<!-- [Missing](docs/missing.md) -->",
        r"\[Missing](docs/missing.md)",
        "````markdown\n> ```\n> [Missing](docs/missing.md)\n````",
        "<!--\n> ~~~\n[Missing](docs/missing.md)\n-->",
        "![sample](docs/missing.md)",
        "![sample][missing]\n\n[missing]: docs/missing.md",
        "![missing][]\n\n[missing]: docs/missing.md",
        "![missing]\n\n[missing]: docs/missing.md",
        "![sample [Missing](docs/missing.md)](img.png)",
        '<span data-link="[Missing](docs/missing.md)">ordinary</span>',
        '<span title="[Missing](docs/missing.md)">ordinary</span>',
        "<div>\n[Missing](docs/missing.md)\n</div>",
        "<script>\n[Missing](docs/missing.md)\n</script>",
        "\n    [Missing](docs/missing.md)",
        "Intro\n[missing]: docs/missing.md\n\n[missing]",
    ],
)
def test_catalogs_follow_visible_navigation_and_ignore_documentation_literals(tmp_path, example):
    project, codex = tmp_path / "project", tmp_path / "codex"
    write(project / "AGENTS.md", "[A1](docs/agent/modules/A1.md)\n" + example)
    write(project / "docs/agent/modules/A1.md", "# A1")

    _, a5, summary = catalogs.build_catalogs(project, codex, "2026-10-05")

    assert summary["project_paths"] == ["AGENTS.md", "docs/agent/modules/A1.md"]
    assert "docs/missing.md" not in a5


@pytest.mark.parametrize("usage", ["[Guide][Current Guide]", "[current guide][]", "[CURRENT GUIDE]"])
def test_catalogs_index_used_reference_targets_and_leave_unused_documents_out(tmp_path, usage):
    project, codex = tmp_path / "project", tmp_path / "codex"
    write(project / "AGENTS.md", "[A1](docs/agent/modules/A1.md)")
    write(
        project / "docs/agent/modules/A1.md",
        "# A1\n"
        + usage
        + '\n\n[current   guide]: <../references/Live Guide.md> "Current guide"\n'
        + "[unused]: ../references/unused.md\n",
    )
    write(project / "docs/agent/references/Live Guide.md", "# Current guide")
    write(project / "docs/agent/references/unused.md", "# Unused guide")

    _, a5, summary = catalogs.build_catalogs(project, codex, "2026-10-05")

    assert summary["project_paths"] == ["AGENTS.md", "docs/agent/modules/A1.md", "docs/agent/references/Live Guide.md"]
    assert "Live Guide.md" in a5 and "unused.md" not in a5


@pytest.mark.parametrize(
    "separator", ["\n\n", "\n \t\n", "\r\n\r\n", "\n~~~\nexample\n~~~\n", "\n# Section\n", "\n> Section\n"]
)
def test_catalogs_follow_visible_links_in_separate_paragraphs_between_unmatched_code_markers(tmp_path, separator):
    write(tmp_path / "AGENTS.md", "` unmatched" + separator + "[Guide](docs/live.md)" + separator + "trailing `\n")
    write(tmp_path / "docs/live.md", "# Guide")

    _, _, summary = catalogs.build_catalogs(tmp_path, tmp_path / "codex", "2026-10-05")

    assert summary["project_paths"] == ["AGENTS.md", "docs/live.md"]


@pytest.mark.parametrize(
    "container",
    [
        "> ```markdown\n> [Missing](docs/missing.md)\n",
        "> > ~~~markdown\n> > [Missing](docs/missing.md)\n",
        "- ```markdown\n  [Missing](docs/missing.md)\n",
        "1. ~~~markdown\n   [Missing](docs/missing.md)\n",
    ],
)
def test_catalogs_ignore_code_in_container_fences(tmp_path, container):
    write(tmp_path / "AGENTS.md", "# Project\n" + container)

    _, _, summary = catalogs.build_catalogs(tmp_path, tmp_path / "codex", "2026-10-05")

    assert summary["project_paths"] == ["AGENTS.md"]


@pytest.mark.parametrize(
    "navigation",
    [
        "ordinary paragraph\n    [Guide](docs/live.md)",
        "ordinary paragraph\n\t[Guide](docs/live.md)",
        "> [Guide](docs/live.md)",
        "- [Guide](docs/live.md)",
        "> ```example``` [Guide](docs/live.md)",
        "- ```example``` [Guide](docs/live.md)",
        '[Guide](docs/live.md "<!-- sample")',
        "[![sample [literal](missing.md)](img.png)](docs/live.md)",
        "Module | Role\n--- | ---\n[Guide](docs/live.md) | Current guide",
    ],
)
def test_catalogs_follow_real_links_with_contextual_markdown_syntax(tmp_path, navigation):
    write(tmp_path / "AGENTS.md", navigation)
    write(tmp_path / "docs/live.md", "# Guide")

    _, _, summary = catalogs.build_catalogs(tmp_path, tmp_path / "codex", "2026-10-05")

    assert summary["project_paths"] == ["AGENTS.md", "docs/live.md"]


def test_catalogs_reject_a_missing_target_of_a_used_reference_link(tmp_path):
    write(tmp_path / "AGENTS.md", "[Guide][required]\n\n[required]: missing.md")

    with pytest.raises(ValueError, match="Missing"):
        catalogs.build_catalogs(tmp_path, tmp_path / "codex", "2026-10-05")


@pytest.mark.parametrize(
    "navigation",
    [
        "[Guide](docs/Guide(one).md)",
        r"[Guide](docs/Guide\(one\).md)",
        '[Guide](<docs/Guide(one).md> "Current guide")',
    ],
)
def test_catalogs_preserve_real_destinations_with_parentheses(tmp_path, navigation):
    project, codex = tmp_path / "project", tmp_path / "codex"
    write(project / "AGENTS.md", navigation)
    write(project / "docs/Guide(one).md", "# Guide")

    _, _, summary = catalogs.build_catalogs(project, codex, "2026-10-05")

    assert summary["project_paths"] == ["AGENTS.md", "docs/Guide(one).md"]


def test_catalogs_use_the_first_reference_definition_without_following_shadowed_targets(tmp_path):
    write(tmp_path / "AGENTS.md", "[Guide][current]\n\n[current]: docs/live.md\n[current]: missing.md")
    write(tmp_path / "docs/live.md", "# Guide")

    _, _, summary = catalogs.build_catalogs(tmp_path, tmp_path / "codex", "2026-10-05")

    assert summary["project_paths"] == ["AGENTS.md", "docs/live.md"]


def test_catalog_script_uses_its_own_checkout_with_an_isolated_interpreter(tmp_path):
    project = tmp_path / "project"
    for name in (
        "scripts/update-instruction-catalogs.py",
        "autostop_manager/__init__.py",
        "autostop_manager/markdown_links.py",
    ):
        write(project / name, (ROOT / name).read_text())
    write(project / "AGENTS.md", "[A1](docs/agent/modules/A1.md)")
    write(project / "docs/agent/modules/A1.md", "# A1")

    result = subprocess.run(
        [sys.executable, "-I", "-B", str(project / "scripts/update-instruction-catalogs.py"), "--help"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "--check" in result.stdout


def test_catalogs_respect_disabled_skill_paths_and_refuse_ambiguous_versions(tmp_path):
    project, codex = tmp_path / "project", tmp_path / "codex"
    path = skill(codex / "skills/.system/core/SKILL.md", "core")
    write(codex / "config.toml", f'[[skills.config]]\npath="{path.parent}"\nenabled=false\n')
    assert catalogs.selected_skills(project, codex) == []
    write(
        codex / "config.toml",
        f'[[skills.config]]\npath="{path.parent}"\nenabled=false\n[plugins."github@openai-curated"]\nenabled=true\n',
    )
    for version in ("1", "2"):
        root = installed_package(codex, "github", version=version, marketplace="openai-curated")
        skill(root / "skills/index/SKILL.md", "index")
    with pytest.raises(ValueError, match="Specify current"):
        catalogs.selected_skills(project, codex)


def test_catalogs_respect_name_only_and_mixed_skill_selectors(tmp_path):
    project, codex = tmp_path / "project", tmp_path / "codex"
    skill(codex / "skills/.system/core/SKILL.md", "core")
    skill(installed_package(codex, "github") / "skills/review/SKILL.md", "review")
    skill(installed_package(codex, "windsor-ai") / "skills/common/SKILL.md", "common")
    retained = skill(installed_package(codex, "build-web-apps") / "skills/build/SKILL.md", "build")
    write(
        codex / "config.toml",
        '[[skills.config]]\nname="core"\nenabled=false\n'
        '[[skills.config]]\nname="github:review"\nenabled=false\n'
        '[[skills.config]]\nname="common"\nenabled=false\n'
        '[[skills.config]]\nname="build-web-apps:build"\nenabled=true\n',
    )
    assert [s["path"] for s in catalogs.selected_skills(project, codex)] == [retained]


@pytest.mark.parametrize("package", ["gmail", "github"])
def test_catalogs_do_not_fallback_from_installed_skill_less_manifest(tmp_path, package):
    project, codex = tmp_path / "project", tmp_path / "codex"
    current = installed_package(codex, package, version="2", skills=None)
    # A leftover directory inside the current version is not declared either.
    skill(current / "skills/undeclared/SKILL.md", "undeclared")
    stale = installed_package(codex, package, marketplace="openai-curated")
    skill(stale / "skills/old/SKILL.md", "old")
    write(codex / "config.toml", f'[plugins."{package}@openai-curated"]\nenabled=true\n')
    before = {p: p.read_bytes() for p in codex.rglob("*") if p.is_file()}

    assert catalogs.selected_skills(project, codex) == []
    assert {p: p.read_bytes() for p in codex.rglob("*") if p.is_file()} == before


def test_catalogs_preserve_installed_manifest_skills_and_ignore_raw_caches(tmp_path):
    project, codex = tmp_path / "project", tmp_path / "codex"
    retained = skill(installed_package(codex, "github") / "skills/current/SKILL.md", "current")
    skill(codex / "plugins/cache/openai-curated/github/stale/skills/old/SKILL.md", "old")
    # Neither the directory nor its manifest is installation metadata by itself.
    uninstalled = installed_package(codex, "gmail", marketplace="openai-curated")
    skill(uninstalled / "skills/cached/SKILL.md", "cached")

    assert [s["path"] for s in catalogs.selected_skills(project, codex)] == [retained]


def test_catalogs_manifest_skills_path_and_default_enabled_metadata(tmp_path):
    root = installed_package(tmp_path, "github", skills="./current-skills")
    retained = skill(root / "current-skills/current/SKILL.md", "current")
    skill(root / "skills/undeclared/SKILL.md", "undeclared")

    assert [s["path"] for s in catalogs.selected_skills(tmp_path / "project", tmp_path)] == [retained]
    assert (
        catalogs.installed_skill_root("github", tmp_path, {"plugins": {"github@openai-curated-remote": {}}})
        == retained.parent.parent
    )


@pytest.mark.parametrize("selector", [None, True, "enabled", {"enabled": "false"}, {"enabled": 0}])
def test_catalogs_reject_invalid_installed_package_selector_types(tmp_path, selector):
    installed_package(tmp_path, "github")
    with pytest.raises(ValueError, match="Invalid installed package selector"):
        catalogs.installed_skill_root("github", tmp_path, {"plugins": {"github@openai-curated-remote": selector}})


def test_catalogs_remote_selector_without_installation_marker_does_not_activate_cache(tmp_path):
    root = installed_package(tmp_path, "github")
    skill(root / "skills/old/SKILL.md", "old")
    (root.parent / ".codex-remote-plugin-install.json").unlink()
    assert catalogs.installed_skill_root("github", tmp_path, {"plugins": {"github@openai-curated-remote": {}}}) is None


@pytest.mark.parametrize("skills", ["", True, [], 1])
def test_catalogs_reject_invalid_installed_manifest_skill_types(tmp_path, skills):
    installed_package(tmp_path, "github", skills=skills)
    with pytest.raises(ValueError, match="Invalid installed package skills"):
        catalogs.selected_skills(tmp_path / "project", tmp_path)


@pytest.mark.parametrize(
    ("current", "legacy", "included"), [(False, True, False), (True, False, True), (None, False, False)]
)
def test_catalogs_respect_actual_installation_selector(tmp_path, current, legacy, included):
    codex = tmp_path / "codex"
    retained = skill(installed_package(codex, "github") / "skills/current/SKILL.md", "current")
    config = {"plugins": {"github@openai-curated": {"enabled": legacy}}}
    if current is not None:
        config["plugins"]["github@openai-curated-remote"] = {"enabled": current}
    root = catalogs.installed_skill_root("github", codex, config)

    assert root == (retained.parent.parent if included else None)


def test_catalogs_reject_ambiguous_installed_versions_even_if_one_has_no_skills(tmp_path):
    installed_package(tmp_path, "github", version="1", skills=None)
    root = installed_package(tmp_path, "github", version="2")
    skill(root / "skills/current/SKILL.md", "current")
    with pytest.raises(ValueError, match="Specify current"):
        catalogs.selected_skills(tmp_path / "project", tmp_path)


@pytest.mark.parametrize(
    "marker", [{}, {"schema_version": 1, "remote_plugin_id": ""}, {"schema_version": 2}, {"schema_version": True}]
)
def test_catalogs_reject_invalid_installation_marker_without_stale_fallback(tmp_path, marker):
    root = installed_package(tmp_path, "github")
    write(root.parent / ".codex-remote-plugin-install.json", json.dumps(marker))
    with pytest.raises(ValueError, match="Invalid installed package marker"):
        catalogs.selected_skills(tmp_path / "project", tmp_path)


def test_catalogs_reject_manifest_skill_path_outside_installed_version(tmp_path):
    installed_package(tmp_path, "github", skills="../../outside")
    with pytest.raises(ValueError, match="outside-package"):
        catalogs.selected_skills(tmp_path / "project", tmp_path)


def test_catalog_check_preserves_recorded_date_after_day_or_year_rollover(tmp_path, monkeypatch):
    project, codex = tmp_path / "project", tmp_path / "codex"
    write(project / "AGENTS.md", "# AutoStop")
    for name in ("A4.md", "A5.md"):
        write(project / "docs/agent/modules" / name, "# Initial")
    a4, a5, _ = catalogs.build_catalogs(project, codex, "2026-12-31")
    write(project / "docs/agent/modules/A4.md", a4)
    write(project / "docs/agent/modules/A5.md", a5)
    monkeypatch.setattr(catalogs, "PROJECT", project)
    monkeypatch.setattr(catalogs, "CODEX", codex)
    monkeypatch.setattr("sys.argv", ["update-instruction-catalogs.py", "--check"])
    assert catalogs.main() == 0
    assert (project / "docs/agent/modules/A4.md").read_text() == a4
    assert (project / "docs/agent/modules/A5.md").read_text() == a5


@pytest.mark.parametrize("external_checked", [False, True])
@pytest.mark.parametrize(
    "suffix",
    [
        "openai-curated-remote/sales/1/skills/index/SKILL.md",
        "openai-curated-remote/pages/1/skills/index/SKILL.md",
        "openai-curated-remote/github/1/skills/index/references/old.md",
    ],
)
def test_catalog_links_reject_retired_packages_and_nonentrypoints(tmp_path, monkeypatch, suffix, external_checked):
    project, codex = tmp_path / "project", tmp_path / "codex"
    monkeypatch.setattr(diagnostics, "EXTERNAL_CODEX_ROOTS", (codex / "skills", codex / "plugins/cache"))
    document = project / diagnostics.MODULE_DOCUMENTS["A4"]
    target = write(codex / "plugins/cache" / suffix, "# old")
    with pytest.raises(ValueError, match="document_link_invalid"):
        diagnostics._local_link_path(str(target), document, project, check_external_links=external_checked)


def test_catalog_link_scope_matches_generator_selected_packages(tmp_path, monkeypatch):
    assert diagnostics.EXTERNAL_SKILL_PACKAGES == frozenset(catalogs.PACKAGES)
    project, codex = tmp_path / "project", tmp_path / "codex"
    monkeypatch.setattr(diagnostics, "EXTERNAL_CODEX_ROOTS", (codex / "skills", codex / "plugins/cache"))
    document = project / diagnostics.MODULE_DOCUMENTS["A5"]
    for package in catalogs.PACKAGES:
        target = skill(codex / f"plugins/cache/openai-curated-remote/{package}/1/skills/index/SKILL.md", "index")
        assert diagnostics._local_link_path(str(target), document, project) == target


def test_roles_are_modules_and_only_link_to_exact_private_journal_entries(tmp_path):
    assert diagnostics.MODULE_PARENTS["M1"] is None
    assert diagnostics.MODULE_PARENTS["M2"] == "M1"
    document = tmp_path / diagnostics.MODULE_DOCUMENTS["M2"]
    assert diagnostics._local_link_path(
        str(diagnostics.ROLE_JOURNAL_ROOT / "journal/INDEX.md"), document, tmp_path, check_external_links=False
    )
    for target in ("/etc/private.md", str(diagnostics.ROLE_JOURNAL_ROOT / "baseline/secrets.md")):
        with pytest.raises(ValueError):
            diagnostics._local_link_path(target, document, tmp_path, check_external_links=False)
    with pytest.raises(ValueError):
        diagnostics._local_link_path(
            str(diagnostics.ROLE_JOURNAL_ROOT / "current-state.md"),
            tmp_path / "AGENTS.md",
            tmp_path,
            check_external_links=False,
        )


def example_record():
    return {
        "id": "019fda71-bd3a-74f2-bade-315ac6827d1a",
        "task": "synthetic task",
        "before": "not checked",
        "actions": "isolated fix",
        "checks": "disposable check passed",
        "result": "source ready",
        "git_sha": "a" * 40,
        "release_required": True,
        "state": "Source ready; live release pending.",
    }


def test_journal_restart_idempotency_rotation_and_year_boundary(tmp_path):
    at = datetime(2026, 12, 31, 23, 59, tzinfo=UTC)
    record = example_record()
    assert journal.run(tmp_path, record, at)["entries"] == 1
    assert journal.run(tmp_path, record, at)["entries"] == 1
    state = (tmp_path / "current-state.md").read_text()
    before = (tmp_path / "journal/2026-W53.md").read_text()
    report = journal.run(tmp_path, at=datetime(2027, 1, 4, tzinfo=UTC))
    assert report["week"] == "2027-W01" and report["entries"] == 0
    assert (tmp_path / "journal/2026-W53.md").read_text() == before
    assert (tmp_path / "current-state.md").read_text() == state
    assert "summaries/2026-W53.md" in (tmp_path / "journal/INDEX.md").read_text()
    assert "1 результатов, 1 коммитов, 1 записей" in (tmp_path / "summaries/2026-W53.md").read_text()
    assert (tmp_path / "current-state.md").stat().st_mode & 0o777 == 0o600
    record["result"] = "different"
    with pytest.raises(ValueError, match="conflict"):
        journal.run(tmp_path, record, at)


@pytest.mark.parametrize("written", [False, True])
def test_journal_recovers_pending_transaction_after_interruption(tmp_path, written):
    at = datetime(2026, 10, 1, tzinfo=UTC)
    record = example_record()
    if written:
        journal.run(tmp_path, record, at)
        (tmp_path / "current-state.md").write_text("old state")
    payload = {**record, "at": at.isoformat()}
    write(tmp_path / ".pending-record.json", json.dumps(payload))

    report = journal.run(tmp_path, at=at)

    assert report["entries"] == 1
    assert record["state"] in (tmp_path / "current-state.md").read_text()
    assert not (tmp_path / ".pending-record.json").exists()


@pytest.mark.parametrize("stage", ["journal", "state", "summary", "index"])
def test_journal_recovers_each_write_boundary_across_iso_year(tmp_path, monkeypatch, stage):
    original = journal.atomic_write
    record = example_record()
    at = datetime(2026, 12, 31, tzinfo=UTC)

    def interrupted(path, text):
        target = (
            (stage == "journal" and path.parent.name == "journal" and "```m2-record" in text)
            or (stage == "state" and path.name == "current-state.md")
            or (stage == "summary" and path.parent.name == "summaries")
            or (stage == "index" and path.name == "INDEX.md")
        )
        if target:
            raise OSError("synthetic interruption")
        original(path, text)

    monkeypatch.setattr(journal, "atomic_write", interrupted)
    with pytest.raises(OSError, match="synthetic"):
        journal.run(tmp_path, record, at)
    assert (tmp_path / ".pending-record.json").exists()
    monkeypatch.setattr(journal, "atomic_write", original)
    report = journal.run(tmp_path, at=datetime(2027, 1, 4, tzinfo=UTC))
    assert report["week"] == "2027-W01" and report["entries"] == 0
    assert len(journal.records(tmp_path / "journal/2026-W53.md")) == 1
    assert record["state"] in (tmp_path / "current-state.md").read_text()
    assert "summaries/2026-W53.md" in (tmp_path / "journal/INDEX.md").read_text()
    assert not (tmp_path / ".pending-record.json").exists()


def test_journal_old_idempotent_replay_preserves_newer_current_state(tmp_path):
    old = example_record()
    current = {**old, "id": "019fda71-bd3a-74f2-bade-315ac6827d1b", "state": "Newer confirmed state."}
    journal.run(tmp_path, old, datetime(2026, 12, 31, tzinfo=UTC))
    at = datetime(2027, 1, 4, tzinfo=UTC)
    journal.run(tmp_path, current, at)
    journal.run(tmp_path, old, at)
    assert "Newer confirmed state." in (tmp_path / "current-state.md").read_text()
    assert len(journal.records(tmp_path / "journal/2026-W53.md")) == 1
    assert len(journal.records(tmp_path / "journal/2027-W01.md")) == 1


@pytest.mark.parametrize("name", ["journal/2026-W40.md", "journal/2026-W39.md", ".pending-record.json"])
def test_journal_rejects_linked_week_or_pending_files(tmp_path, name):
    outside = tmp_path / "external.txt"
    outside.write_text("[]")
    link = tmp_path / name
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(outside)
    with pytest.raises(ValueError, match="journal_symlink"):
        journal.run(tmp_path, at=datetime(2026, 10, 1, tzinfo=UTC))


@pytest.mark.parametrize("fault", ["sha", "release", "empty", "fields", "naive", "symlink"])
def test_journal_rejects_invalid_record_or_path(tmp_path, fault):
    record, at = example_record(), datetime(2026, 10, 1, tzinfo=UTC)
    if fault == "sha":
        record["git_sha"] = "fake"
    elif fault == "release":
        record["release_required"] = "yes"
    elif fault == "empty":
        record["task"] = " "
    elif fault == "fields":
        record["unknown"] = "not allowed"
    elif fault == "naive":
        at = at.replace(tzinfo=None)
    else:
        (tmp_path / "journal").symlink_to(tmp_path)
    with pytest.raises(ValueError):
        journal.run(tmp_path, record, at)
