"""Mini Git/venv snapshots only; no services, production paths or network."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace
import venv

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/refresh-manager-runtime-workspace.py"
SPEC = importlib.util.spec_from_file_location("runtime_workspace", SCRIPT)
assert SPEC and SPEC.loader
converter = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(converter)


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], stderr=subprocess.PIPE).decode().strip()


def seal(root):
    paths = sorted(p for p in root.rglob("*") if p.is_file() and p.name != "MANIFEST.sha256")
    (root / "MANIFEST.sha256").write_text(
        "".join(hashlib.sha256(p.read_bytes()).hexdigest() + "  ./" + str(p.relative_to(root)) + "\n" for p in paths)
    )


@pytest.fixture
def layout(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    git(workspace, "init", "-b", "source")
    git(workspace, "config", "user.email", "synthetic@localhost")
    git(workspace, "config", "user.name", "Synthetic")
    for name, content in {
        "AGENTS.md": "Old synthetic instructions\n",
        "docs/A1.md": "Old navigation\n",
        "autostop_manager/__init__.py": "__version__ = 'old'\n",
        ".gitignore": ".env\n.env.local\n.crm-mcp.env\ndata/\n.venv/\n__pycache__/\n",
        ".gitattributes": "*.md text\n",
    }.items():
        path = workspace / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    git(workspace, "add", ".")
    git(workspace, "commit", "-m", "Synthetic source")
    old = git(workspace, "rev-parse", "HEAD")
    git(workspace, "checkout", "--detach", old)
    for name in [".env", "private-documents/note.txt", ".codex-attachments/fake.txt", "data/state.txt"]:
        path = workspace / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("Synthetic protected bytes\n")
    release = tmp_path / "releases" / "sealed"
    release.mkdir(parents=True)
    for path in [
        p
        for p in workspace.rglob("*")
        if p.is_file() and p.parts[-1] in {"AGENTS.md", "A1.md", "__init__.py", ".gitignore", ".gitattributes"}
    ]:
        relative = path.relative_to(workspace)
        target = release / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(path.read_bytes().replace(b"Old", b"Current"))
    (release / "REVISION").write_text("a" * 40 + "\n")
    seal(release)
    current = release.parent / "current"
    current.symlink_to(release, target_is_directory=True)
    runtime = tmp_path / "cold-venv"
    venv.EnvBuilder(with_pip=False).create(runtime)
    legacy = workspace / ".venv"
    venv.EnvBuilder(with_pip=False).create(legacy)
    python = runtime / "bin/python"
    monkeypatch.setattr(converter, "_service_python", lambda: python)
    monkeypatch.setattr(converter, "_references", lambda _paths: [])
    return SimpleNamespace(
        workspace=workspace,
        release=release,
        current=current,
        runtime=runtime,
        python=python,
        legacy=legacy,
        old=old,
        receipts=tmp_path / "receipt",
    )


def plan(layout):
    return converter.build_plan(layout.workspace, layout.current, layout.python)


def apply(layout, value=None, **options):
    return converter.apply_plan(value or plan(layout), layout.old, layout.receipts, **options)


def test_plan_reads_only_and_records_actual_runtime(layout):
    before = (layout.workspace / ".git/index").read_bytes()
    value = plan(layout)
    assert value["old_head"] == layout.old
    assert value["runtime_venv"] == str(layout.runtime)
    assert value["remove_legacy_venv_required"] is True
    assert (layout.workspace / ".git/index").read_bytes() == before
    assert git(layout.workspace, "rev-parse", "HEAD") == layout.old
    assert not layout.receipts.exists()


def test_physical_venv_requires_separate_flag_before_mutation(layout):
    with pytest.raises(converter.Refused, match="explicit_removal_flag"):
        apply(layout)
    assert not layout.receipts.exists()
    assert layout.legacy.is_dir() and not layout.legacy.is_symlink()


def test_conversion_preserves_data_git_history_private_metadata_and_ignore(layout):
    value = plan(layout)
    result = apply(layout, value, remove_legacy_venv=True)
    assert result["ok"] and result["staging_removed"]
    assert converter._preserved(layout.workspace, value["entries"]) == value["preserved"]
    assert git(layout.workspace, "rev-parse", "refs/heads/source") == layout.old
    assert git(layout.workspace, "symbolic-ref", "HEAD") == converter.BRANCH
    assert git(layout.workspace, "rev-parse", "HEAD^") == layout.old
    for name in value["entries"]:
        path = layout.workspace / name
        if name in converter.CONTROLS:
            assert not path.is_symlink()
            assert path.read_bytes() == (layout.release / name).read_bytes()
        else:
            assert path.is_symlink() and os.readlink(path) == str(layout.current / name)
    assert layout.legacy.is_dir() and not layout.legacy.is_symlink()
    assert (layout.legacy / converter.SHIM_MARKER).is_file()
    version = ".".join(str(n) for n in value["runtime"]["version"])
    assert set(result["lexical_python_probes"]) == {"bin/python", "bin/python3", "bin/python" + version}
    assert all(Path(p["prefix"]).resolve() == layout.runtime for p in result["lexical_python_probes"].values())
    assert git(layout.workspace, "check-ignore", ".env", "data/state.txt").splitlines() == [".env", "data/state.txt"]
    assert not git(layout.workspace, "status", "--porcelain", "--untracked-files=no")
    assert json.loads((layout.receipts / "after.json").read_text())["preserved_verified"]


@pytest.mark.parametrize("staged", [False, True])
def test_dirty_tracked_files_block_conversion(layout, staged):
    (layout.workspace / "AGENTS.md").write_text("Foreign edit\n")
    if staged:
        git(layout.workspace, "add", "AGENTS.md")
    with pytest.raises(converter.Refused, match="tracked_changes"):
        plan(layout)
    assert (layout.workspace / "AGENTS.md").read_text() == "Foreign edit\n"


def test_assume_unchanged_does_not_hide_raw_blob_edit(layout):
    git(layout.workspace, "update-index", "--assume-unchanged", "AGENTS.md")
    (layout.workspace / "AGENTS.md").write_text("Foreign hidden edit\n")
    with pytest.raises(converter.Refused, match="tracked_blob_changed"):
        plan(layout)


@pytest.mark.parametrize("relative", ["docs/__pycache__/fake.pyc", "docs/empty-private-directory"])
def test_ignored_or_empty_untracked_descendants_are_never_deleted(layout, relative):
    path = layout.workspace / relative
    if path.suffix:
        path.parent.mkdir()
        path.write_bytes(b"Private untracked bytes")
    else:
        path.mkdir()
    with pytest.raises(converter.Refused, match="untracked_descendant"):
        plan(layout)
    assert path.exists()


def test_expected_head_fences_plan(layout):
    value = plan(layout)
    with pytest.raises(converter.Refused, match="expected_head"):
        converter.apply_plan(value, "0" * 40, layout.receipts, remove_legacy_venv=True)
    assert not layout.receipts.exists()


def test_release_hash_change_blocks_apply(layout):
    value = plan(layout)
    (layout.release / "AGENTS.md").write_text("Changed sealed file\n")
    with pytest.raises(converter.Refused, match="manifest_mismatch"):
        apply(layout, value, remove_legacy_venv=True)


def test_unmanifested_file_is_not_an_approved_release(layout):
    (layout.release / "unsealed.py").write_text("not sealed\n")
    with pytest.raises(converter.Refused, match="manifest_coverage"):
        plan(layout)


def test_busy_legacy_venv_blocks_even_explicit_removal(layout, monkeypatch):
    monkeypatch.setattr(converter, "_references", lambda paths: [{"pid": 7, "path": str(paths[-1])}])
    with pytest.raises(converter.Refused, match="entries_in_use"):
        apply(layout, remove_legacy_venv=True)
    assert layout.legacy.is_dir() and not layout.legacy.is_symlink()


def test_receipt_parent_is_private_before_mutation(layout, tmp_path):
    parent = tmp_path / "public"
    parent.mkdir(mode=0o755)
    with pytest.raises(converter.Refused, match="private"):
        converter.apply_plan(plan(layout), layout.old, parent / "receipt", remove_legacy_venv=True)
    assert not (parent / "receipt").exists()


def test_precommit_failure_restores_original_entries_and_refs(layout, monkeypatch):
    value = plan(layout)
    original = converter._runtime
    calls = 0

    def fail_late(*args):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise converter.Refused("synthetic_late_runtime_failure")
        return original(*args)

    monkeypatch.setattr(converter, "_runtime", fail_late)
    with pytest.raises(converter.Refused, match="synthetic_late"):
        apply(layout, value, remove_legacy_venv=True)
    assert (layout.workspace / "AGENTS.md").read_text() == "Old synthetic instructions\n"
    assert layout.legacy.is_dir() and not layout.legacy.is_symlink()
    assert git(layout.workspace, "rev-parse", "HEAD") == layout.old
    assert json.loads((layout.receipts / "after.json").read_text())["rollback_complete"]


def test_postcommit_partial_cleanup_failure_keeps_healthy_alias_workspace(layout, monkeypatch):
    value = plan(layout)
    original = converter.shutil.rmtree
    removed = []

    def fail_second_directory(path, *args, **kwargs):
        if Path(path).name not in {*value["old_entries"], ".venv"}:
            return original(path, *args, **kwargs)
        if removed:
            raise OSError("synthetic cleanup failure after one real removal")
        original(path, *args, **kwargs)
        removed.append(str(path))
        return None

    fail_second_directory.avoids_symlink_attacks = original.avoids_symlink_attacks
    monkeypatch.setattr(converter.shutil, "rmtree", fail_second_directory)
    with pytest.raises(OSError, match="cleanup failure"):
        apply(layout, value, remove_legacy_venv=True)
    assert removed
    assert (layout.workspace / "AGENTS.md").is_symlink()
    assert (layout.workspace / "AGENTS.md").read_bytes() == (layout.release / "AGENTS.md").read_bytes()
    assert git(layout.workspace, "symbolic-ref", "HEAD") == converter.BRANCH
    assert not git(layout.workspace, "status", "--porcelain", "--untracked-files=no")
    after = json.loads((layout.receipts / "after.json").read_text())
    assert after["converted"] and after["cleanup_pending"] and not after["ok"]
    assert Path(after["retained_staging"]).is_dir()
    committed = json.loads((layout.receipts / "committed.json").read_text())
    assert committed["new_head"] == git(layout.workspace, "rev-parse", "HEAD")
    assert committed["staging"] == after["retained_staging"]


def test_other_ops_branch_is_never_moved(layout):
    git(layout.workspace, "branch", "ops/runtime-workspace", layout.old)
    with pytest.raises(converter.Refused, match="ops_branch_already_owned"):
        plan(layout)
    assert git(layout.workspace, "rev-parse", converter.BRANCH) == layout.old


def test_detached_head_race_is_detected_and_foreign_head_is_preserved(layout, monkeypatch):
    other = git(
        layout.workspace,
        "commit-tree",
        git(layout.workspace, "rev-parse", "HEAD^{tree}"),
        "-p",
        layout.old,
        "-m",
        "Foreign detached HEAD",
    )
    original = converter.Conversion.check_fences

    def race(self):
        (self.gitdir / "HEAD").write_text(other + "\n")
        original(self)

    monkeypatch.setattr(converter.Conversion, "check_fences", race)
    with pytest.raises(converter.Refused, match="head_changed"):
        apply(layout, remove_legacy_venv=True)
    assert git(layout.workspace, "rev-parse", "HEAD") == other
    assert (layout.workspace / "AGENTS.md").read_text() == "Old synthetic instructions\n"
    assert json.loads((layout.receipts / "after.json").read_text())["rollback_complete"]


def test_rollback_preserves_foreign_file_and_retains_original_in_staging(layout, monkeypatch):
    def race(self):
        path = self.workspace / "AGENTS.md"
        path.unlink()
        path.write_text("Foreign concurrent edit\n")
        raise converter.Refused("synthetic_concurrent_edit")

    monkeypatch.setattr(converter.Conversion, "check_fences", race)
    with pytest.raises(converter.Refused, match="concurrent_edit"):
        apply(layout, remove_legacy_venv=True)
    assert (layout.workspace / "AGENTS.md").read_text() == "Foreign concurrent edit\n"
    after = json.loads((layout.receipts / "after.json").read_text())
    assert not after["rollback_complete"] and "AGENTS.md" in after["conflicts"]
    assert (Path(after["retained_staging"]) / "AGENTS.md").read_text() == "Old synthetic instructions\n"


def test_finally_never_removes_foreign_git_lock(layout, monkeypatch):
    original = converter.Conversion.cleanup
    lock = layout.workspace / ".git/index.lock"

    def race(self):
        lock.write_text("Foreign Git writer\n")
        original(self)

    monkeypatch.setattr(converter.Conversion, "cleanup", race)
    assert apply(layout, remove_legacy_venv=True)["ok"]
    assert lock.read_text() == "Foreign Git writer\n"


def test_actual_ignore_failure_rolls_back_published_head_and_index(layout):
    (layout.release / ".gitignore").write_text(".env\n.env.local\n.crm-mcp.env\n")
    seal(layout.release)
    with pytest.raises(converter.Refused, match="protected_ignore_rules_missing"):
        apply(layout, remove_legacy_venv=True)
    assert git(layout.workspace, "rev-parse", "HEAD") == layout.old
    assert git(layout.workspace, "rev-parse", "--symbolic-full-name", "HEAD") == "HEAD"
    assert not git(layout.workspace, "status", "--porcelain", "--untracked-files=no")
    assert json.loads((layout.receipts / "after.json").read_text())["rollback_complete"]


def test_owned_ops_branch_can_be_refreshed_without_venv_removal_flag(layout):
    assert apply(layout, remove_legacy_venv=True)["ok"]
    value = plan(layout)
    assert not value["remove_legacy_venv_required"]
    result = converter.apply_plan(value, value["old_head"], layout.receipts.parent / "second-receipt")
    assert result["ok"] and not result["legacy_venv_removed"]
    assert git(layout.workspace, "rev-parse", "refs/heads/source") == layout.old


def test_broken_future_lexical_python_rolls_back_before_commit_or_deletion(layout, monkeypatch):
    original = converter._python_probe
    old_identity = converter._identity(layout.legacy)

    def fail_generic_alias(python, release):
        if python == layout.legacy / "bin/python3":
            raise converter.Refused("synthetic_portable_python_basename_failure")
        return original(python, release)

    monkeypatch.setattr(converter, "_python_probe", fail_generic_alias)
    with pytest.raises(converter.Refused, match="lexical_python_probe_failed:bin/python3"):
        apply(layout, remove_legacy_venv=True)
    assert converter._identity(layout.legacy) == old_identity
    assert (layout.workspace / "AGENTS.md").read_text() == "Old synthetic instructions\n"
    assert git(layout.workspace, "rev-parse", "HEAD") == layout.old
    assert not (layout.receipts / "committed.json").exists()
    assert json.loads((layout.receipts / "after.json").read_text())["rollback_complete"]


def test_own_shim_never_hides_foreign_files_on_refresh(layout):
    assert apply(layout, remove_legacy_venv=True)["ok"]
    private = layout.legacy / "private-untracked.txt"
    private.write_text("Preserve foreign content\n")
    with pytest.raises(converter.Refused, match="untracked_descendant"):
        plan(layout)
    assert private.read_text() == "Preserve foreign content\n"


@pytest.mark.parametrize("portable", [False, True])
def test_development_hook_must_select_source_worktree_runtime(layout, portable):
    python = '"$PROJECT_DIR/.venv/bin/python"' if portable else str(layout.legacy / "bin/python")
    hook = layout.workspace / ".git/hooks/pre-commit"
    hook.write_text("#!/bin/bash\nINSTALL_PYTHON=" + python + "\n")
    if portable:
        assert plan(layout)["old_head"] == layout.old
    else:
        with pytest.raises(converter.Refused, match="development_hook_requires_source_worktree_runtime"):
            plan(layout)
    assert not layout.receipts.exists()


def test_shim_quotes_shared_python_path_and_preserves_arguments(layout, monkeypatch):
    runtime = layout.runtime.with_name("cold venv with spaces")
    layout.runtime.rename(runtime)
    layout.runtime, layout.python = runtime, runtime / "bin/python"
    monkeypatch.setattr(converter, "_service_python", lambda: layout.python)
    assert apply(layout, remove_legacy_venv=True)["ok"]
    args = ["space argument", "$literal; ' quoted"]
    output = subprocess.check_output(
        [
            str(layout.legacy / "bin/python"),
            "-I",
            "-B",
            "-c",
            "import json,sys;print(json.dumps(sys.argv[1:]))",
            *args,
        ]
    )
    assert json.loads(output) == args


def test_partial_shim_build_failure_never_changes_original_workspace(layout, monkeypatch):
    value = plan(layout)
    old_identity = converter._identity(layout.legacy)
    index = (layout.workspace / ".git/index").read_bytes()
    original = converter._write_regular
    wrappers = 0

    def fail_second_wrapper(path, content, mode):
        nonlocal wrappers
        if path.parent.name == "bin":
            wrappers += 1
            if wrappers == 2:
                raise OSError("synthetic partial shim write")
        return original(path, content, mode)

    monkeypatch.setattr(converter, "_write_regular", fail_second_wrapper)
    with pytest.raises(OSError, match="partial shim write"):
        apply(layout, value, remove_legacy_venv=True)
    assert wrappers == 2
    assert converter._identity(layout.legacy) == old_identity
    assert (layout.workspace / ".git/index").read_bytes() == index
    assert git(layout.workspace, "rev-parse", "HEAD") == layout.old
    assert (layout.workspace / "AGENTS.md").read_text() == "Old synthetic instructions\n"
    assert not (layout.receipts / "committed.json").exists()
    assert json.loads((layout.receipts / "after.json").read_text())["rollback_complete"]
