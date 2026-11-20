from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

FILES = (
    "crm_backup_coordinator.py",
    "crm-snapshot.py",
    "check-crm-snapshot-restore.py",
    "deliver-crm-backup.py",
    "transfer-crm-backup-windows.py",
)
SCRIPT = Path(__file__).resolve().parents[1] / "scripts/verify-crm-backup-package.py"


@pytest.fixture
def candidate(tmp_path):
    source = tmp_path / "manager"
    source.mkdir()
    scripts = source / "scripts"
    scripts.mkdir()
    package = tmp_path / "package"
    package.mkdir()
    verifier = scripts / SCRIPT.name
    shutil.copyfile(SCRIPT, verifier)

    def git(*args):
        return subprocess.check_output(
            [
                "git",
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "commit.gpgsign=false",
                "-c",
                "user.name=Synthetic",
                "-c",
                "user.email=synthetic@example.invalid",
                *args,
            ],
            cwd=source,
            text=True,
        ).strip()

    git("init", "--quiet", "--template=")
    manifest = {}
    for name in FILES:
        body = f"# {name} fixture\n".encode("ascii")
        (scripts / name).write_bytes(body)
        (package / name).write_bytes(body)
        manifest[name] = hashlib.sha256(body).hexdigest()
    git("add", "scripts")
    git("commit", "--quiet", "-m", "Pinned package source")
    revision = git("rev-parse", "HEAD")
    (package / "REVISION").write_text(revision + "\n", encoding="ascii")
    (package / "source-manifest.json").write_text(json.dumps(manifest), encoding="ascii")

    def verify(expected_revision=revision):
        return subprocess.run(
            [sys.executable, str(verifier), "--revision", expected_revision, "--package-dir", str(package)],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

    return package, scripts, git, revision, manifest, verify


def test_exact_package_reads_the_pinned_commit_and_keeps_files_unchanged(candidate):
    package, scripts, git, revision, _, verify = candidate
    (scripts / "crm-snapshot.py").write_text("# newer committed content\n", encoding="ascii")
    git("add", "scripts")
    git("commit", "--quiet", "-m", "Newer revision")
    (scripts / "crm-snapshot.py").write_text("# dirty change\n", encoding="ascii")
    (package / "__pycache__").mkdir()
    before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in package.iterdir() if path.is_file()}

    result = verify()

    assert result.returncode == 0
    assert result.stderr == ""
    assert json.loads(result.stdout) == {"ok": True, "source_revision": revision, "files": 5}
    assert {
        path: (path.read_bytes(), path.stat().st_mtime_ns) for path in package.iterdir() if path.is_file()
    } == before


@pytest.mark.parametrize("offset", ["manifest", "source"])
def test_detects_known_manifest_drift_and_self_consistent_wrong_bytes(candidate, offset):
    package, _, _, _, manifest, verify = candidate
    filename = "crm-snapshot.py"
    updated = b"# synthetic replacement\n"
    manifest[filename] = hashlib.sha256(updated).hexdigest()
    if offset == "source":
        (package / filename).write_bytes(updated)
    (package / "source-manifest.json").write_text(json.dumps(manifest), encoding="ascii")

    result = verify()

    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr.strip() == f"package_{offset}_hash_mismatch:{filename}"


@pytest.mark.parametrize(
    ("change", "error"),
    [
        ("different_revision", "package_revision_mismatch"),
        ("short_revision", "package_source_revision_invalid"),
        ("unknown_revision", "package_source_revision_unavailable"),
        ("extra_key", "package_manifest_files_mismatch"),
        ("duplicate_key", "package_manifest_invalid"),
        ("missing_file", "package_file_unreadable:crm-snapshot.py"),
    ],
)
def test_rejects_invalid_package_identity(candidate, change, error):
    package, _, _, revision, manifest, verify = candidate
    requested = revision
    manifest_path = package / "source-manifest.json"
    if change == "different_revision":
        (package / "REVISION").write_text("0" * 40, encoding="ascii")
    elif change == "short_revision":
        requested = revision[:12]
    elif change == "unknown_revision":
        requested = "0" * 40
    elif change == "extra_key":
        manifest["unexpected.py"] = "0" * 64
        manifest_path.write_text(json.dumps(manifest), encoding="ascii")
    elif change == "duplicate_key":
        first_key = FILES[0]
        content = json.dumps(manifest)[:-1] + f', "{first_key}": "{manifest[first_key]}"}}'
        manifest_path.write_text(content, encoding="ascii")
    else:
        (package / "crm-snapshot.py").unlink()

    result = verify(requested)

    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr.strip() == error
