from __future__ import annotations

import argparse
import gzip
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import tarfile
import zipfile

import pytest

PREPARER = Path(__file__).resolve().parents[1] / "scripts/prepare-automotive-offline.py"
spec = importlib.util.spec_from_file_location("automotive_offline_preparer", PREPARER)
assert spec and spec.loader
preparer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preparer)


@pytest.fixture
def archives(tmp_path, monkeypatch):
    source = tmp_path / "source"
    (source / "docs/agent").mkdir(parents=True)
    database = tmp_path / "fixture.db"
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE Wmi (id integer)")
        db.execute("CREATE TABLE Pattern (id integer)")
        db.execute("INSERT INTO Wmi VALUES (1)")
        db.execute("INSERT INTO Pattern VALUES (1)")
    corgi = tmp_path / "fixture.tgz"
    with tarfile.open(corgi, "w:gz") as archive:
        for name, data in [
            ("package/dist/browser.mjs", b"export async function decodeVIN() {}"),
            ("package/LICENSE", b"ISC fixture license"),
            ("package/dist/db/vpic.lite.db.gz", gzip.compress(database.read_bytes())),
        ]:
            entry = tarfile.TarInfo(name)
            entry.size = len(data)
            archive.addfile(entry, io.BytesIO(data))
    wheel = tmp_path / "fixture.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("vininfo/__init__.py", "VERSION='1.11.0'\n")
        archive.writestr("vininfo-1.11.0.dist-info/licenses/LICENSE", "BSD-3-Clause fixture license")
    metadata = {}
    for name, archive, version in [("corgi", corgi, "2.0.4"), ("vininfo", wheel, "1.11.0")]:
        metadata[name] = {
            "archive_url": "https://example.com/" + archive.name,
            "archive_bytes": archive.stat().st_size,
            "archive_sha256": preparer.sha256(archive),
            "version": version,
        }
    metadata["corgi"].update({"database_bytes": database.stat().st_size, "database_sha256": preparer.sha256(database)})
    (source / "docs/agent/automotive_offline_sources.json").write_text(json.dumps(metadata))
    monkeypatch.setattr(preparer, "SOURCE_ROOT", source)
    monkeypatch.setattr(preparer.shutil, "which", lambda _: sys.executable)
    monkeypatch.setattr(preparer.subprocess, "check_output", lambda *args, **kwargs: "v24.21.0\n")
    args = argparse.Namespace(
        output=str(tmp_path / "runtime"), corgi_archive=str(corgi), vininfo_wheel=str(wheel), node="node"
    )
    return args, metadata


def test_preparation_from_locked_archives_requires_no_network_and_preserves_licenses(archives, monkeypatch):
    args, _ = archives
    monkeypatch.setattr(preparer.urllib.request, "urlopen", lambda *_args, **_kwargs: pytest.fail("network forbidden"))
    receipt = preparer.prepare(args)
    root = Path(args.output)
    manifest = json.loads((root / "manifest.json").read_text())
    assert receipt["ok"] is True and receipt["activated"] is False
    assert receipt["record_counts"] == {"Wmi": 1, "Pattern": 1}
    assert manifest["node"]["sha256"] == preparer.sha256(Path(sys.executable))
    assert (root / "licenses/Corgi-ISC.txt").read_text() == "ISC fixture license"
    assert (root / "python-packages/vininfo-1.11.0.dist-info/licenses/LICENSE").is_file()
    assert (root.stat().st_mode & 0o777) == 0o700
    assert all((path.stat().st_mode & 0o777) == 0o600 for path in root.rglob("*") if path.is_file())
    assert all(preparer.sha256(root / name) == digest for name, digest in manifest["files"].items())


def test_preparation_never_replaces_an_existing_runtime(archives):
    args, _ = archives
    Path(args.output).mkdir()
    (Path(args.output) / "keep").write_text("existing")
    with pytest.raises(ValueError, match="output_must_be_new"):
        preparer.prepare(args)
    assert (Path(args.output) / "keep").read_text() == "existing"


def test_preparation_rejects_tampered_archive_before_import(archives):
    args, _ = archives
    Path(args.corgi_archive).write_bytes(b"tampered")
    with pytest.raises(ValueError, match="archive_hash_mismatch"):
        preparer.prepare(args)
    assert not (Path(args.output) / "manifest.json").exists()


def test_preparation_requires_capacity_before_creating_output(archives, monkeypatch):
    args, _ = archives
    monkeypatch.setattr(preparer.shutil, "disk_usage", lambda _: shutil._ntuple_diskusage(1_000_000, 999_999, 1))
    with pytest.raises(ValueError, match="insufficient_capacity"):
        preparer.prepare(args)
    assert not Path(args.output).exists()


def test_preparation_rejects_database_hash_mismatch(archives):
    args, metadata = archives
    metadata["corgi"]["database_sha256"] = "0" * 64
    (preparer.SOURCE_ROOT / "docs/agent/automotive_offline_sources.json").write_text(json.dumps(metadata))
    with pytest.raises(ValueError, match="database_hash_mismatch"):
        preparer.prepare(args)
    assert not (Path(args.output) / "manifest.json").exists()


def test_preparation_rejects_unsafe_wheel_member(archives):
    args, metadata = archives
    wheel = Path(args.vininfo_wheel)
    with zipfile.ZipFile(wheel, "a") as archive:
        archive.writestr("vininfo/../../escape.txt", "unsafe")
    metadata["vininfo"].update(
        {"archive_bytes": wheel.stat().st_size, "archive_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest()}
    )
    (preparer.SOURCE_ROOT / "docs/agent/automotive_offline_sources.json").write_text(json.dumps(metadata))
    with pytest.raises(ValueError, match="invalid_vininfo_wheel_member"):
        preparer.prepare(args)
    assert not (Path(args.output).parent / "escape.txt").exists()
    assert not (Path(args.output) / "manifest.json").exists()


def test_preparation_requires_node_sqlite_runtime(archives, monkeypatch):
    args, _ = archives
    monkeypatch.setattr(preparer.subprocess, "check_output", lambda *args, **kwargs: "v20.20.0\n")
    with pytest.raises(ValueError, match="requires_22_13"):
        preparer.prepare(args)
    assert not Path(args.output).exists()
