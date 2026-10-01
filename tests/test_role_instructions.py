from __future__ import annotations

from datetime import datetime, UTC
import importlib.util
import json
from pathlib import Path

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
        folder = cache / package / "1/skills/index"
        skill(folder / "SKILL.md", "index")
        write(folder / "references/old.md", "# should not be indexed")
        skill(folder / "nested/SKILL.md", "nested")
    for package in ("sales", "pages", "work-pets", "openai-templates", "figma"):
        skill(cache / package / "1/skills/index/SKILL.md", "index")
    write(codex / "config.toml", '[plugins."github@openai-curated"]\nenabled=false\n')
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


def test_catalogs_respect_disabled_skill_paths_and_refuse_ambiguous_versions(tmp_path):
    project, codex = tmp_path / "project", tmp_path / "codex"
    path = skill(codex / "skills/.system/core/SKILL.md", "core")
    write(codex / "config.toml", f'[[skills.config]]\npath="{path.parent}"\nenabled=false\n')
    assert catalogs.selected_skills(project, codex) == []
    for version in ("1", "2"):
        skill(codex / f"plugins/cache/openai-curated/github/{version}/skills/index/SKILL.md", "index")
    with pytest.raises(ValueError, match="Specify current"):
        catalogs.selected_skills(project, codex)


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
