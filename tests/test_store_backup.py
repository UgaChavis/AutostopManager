"""Synthetic boundaries for the one-generation host backup helper."""

from datetime import UTC, datetime
import errno
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "store_backup", Path(__file__).resolve().parents[1] / "scripts/store-backup.py"
)
backup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(backup)
inspect_containers = backup.container_identity


class StoreBackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "backups"
        self.root.mkdir(mode=0o700)
        self.source = self.base / "source.txt"
        self.source.write_text("synthetic data")
        self.source.chmod(0o640)
        self.old = self.root / "20260101T000000Z-123"
        self.old.mkdir(mode=0o700)
        (self.old / "manifest.json").write_text(json.dumps({"schema": backup.SCHEMA, "complete": True}))
        (self.old / "database.dump").write_bytes(b"old valid backup")
        self.sources = patch.object(backup, "SOURCES", {"files/source.txt": self.source})
        self.sources.start()
        self.addCleanup(self.sources.stop)
        self.identity = patch.object(backup, "container_identity", return_value={"db": "synthetic"})
        self.identity.start()
        self.addCleanup(self.identity.stop)
        self.commands = []

    def command(self, args, *, input_file=None, output_file=None):
        self.commands.append(args)
        if output_file is not None:
            output_file.write(b"PGDMP synthetic archive")

    def create(self, command=None):
        with patch.object(backup, "quiet_run", side_effect=command or self.command):
            return backup.create_backup(self.root, self.base / "backup.lock")

    def test_success_validates_before_replacing_the_only_generation(self):
        result = self.create()
        self.assertTrue(result["ok"])
        self.assertFalse(self.old.exists())
        current = Path(result["backup"])
        self.assertEqual(list(self.root.iterdir()), [current])
        manifest = json.loads((current / "manifest.json").read_text())
        self.assertTrue(manifest["validation"]["source_stable_across_dump"])
        self.assertTrue(any("--file=/dev/null" in args for args in self.commands))
        self.assertFalse(any("-d" in args for args in self.commands))

    def test_partial_retirement_preserves_publication_and_allows_next_capture(self):
        for interrupted in (False, True):
            with self.subTest(sigterm=interrupted):
                root = self.base / ("sigterm" if interrupted else "io-error")
                root.mkdir(mode=0o700)
                old = root / self.old.name
                shutil.copytree(self.old, old)
                unrelated = root / "unique-work"
                unrelated.mkdir()
                published = root / "20261124T033000Z-4321"
                latest = root / "20261125T033000Z-4321"

                def stop_removal(path, *, root=root, published=published, interrupted=interrupted):
                    holder = Path(path)
                    self.assertEqual(holder.parent, root)
                    self.assertTrue(json.loads((published / "manifest.json").read_text())["complete"])
                    (manifest,) = holder.rglob("manifest.json")
                    manifest.unlink()
                    if interrupted:
                        backup.terminate(signal.SIGTERM, None)
                    raise OSError(errno.EIO, "synthetic error after removing the old manifest")

                with (
                    patch.object(backup, "datetime") as clock,
                    patch.object(backup.os, "getpid", return_value=4321),
                    patch.object(backup, "quiet_run", side_effect=self.command),
                ):
                    clock.now.return_value = datetime(2026, 11, 24, 3, 30, tzinfo=UTC)
                    with patch.object(backup.shutil, "rmtree", side_effect=stop_removal):
                        if interrupted:
                            with self.assertRaises(SystemExit) as stopped:
                                backup.create_backup(root, self.base / "backup.lock")
                            self.assertEqual(stopped.exception.code, 128 + signal.SIGTERM)
                        else:
                            result = backup.create_backup(root, self.base / "backup.lock")
                            self.assertFalse(result["ok"])
                            self.assertEqual(Path(result["backup"]), published)
                            self.assertEqual(result["manifest_sha256"], backup.digest(published / "manifest.json"))
                            self.assertEqual(result["retired_generations"], [])
                            (error,) = result["rotation_errors"]
                            self.assertEqual(error["generation"], old.name)
                            self.assertRegex(error["reason"], r"^[a-zA-Z][a-zA-Z0-9_]*$")

                    (residue,) = set(root.iterdir()) - {published, unrelated}
                    self.assertRegex(residue.name, rf"^\.retiring-{old.name}-.+$")
                    self.assertFalse(old.exists())
                    self.assertEqual(list(residue.rglob("manifest.json")), [])
                    if not interrupted:
                        self.assertEqual(result["rotation_residue"], [residue.name])

                    clock.now.return_value = datetime(2026, 11, 25, 3, 30, tzinfo=UTC)
                    result = backup.create_backup(root, self.base / "backup.lock")

                self.assertFalse(result["ok"])
                self.assertEqual(Path(result["backup"]), latest)
                self.assertTrue(json.loads((latest / "manifest.json").read_text())["complete"])
                self.assertEqual(result["manifest_sha256"], backup.digest(latest / "manifest.json"))
                self.assertEqual(result["retired_generations"], [published.name])
                self.assertEqual(result["rotation_residue"], [residue.name])
                self.assertEqual(set(root.iterdir()), {latest, unrelated, residue})

    def test_missing_manifest_preserves_orphan_and_retires_valid_previous(self):
        orphan = self.root / "20251231T235959Z-123"
        orphan.mkdir(mode=0o700)
        sentinel = orphan / "database.dump"
        sentinel.write_bytes(b"unknown synthetic file")
        actual_iterdir = Path.iterdir

        def oldest_first(path):
            entries = actual_iterdir(path)
            return iter(sorted(entries)) if path == self.root else entries

        with patch.object(Path, "iterdir", oldest_first):
            result = self.create()

        current = Path(result["backup"])
        self.assertFalse(result["ok"])
        self.assertEqual(result["manifest_sha256"], backup.digest(current / "manifest.json"))
        self.assertEqual(result["retired_generations"], [self.old.name])
        self.assertEqual(
            result["rotation_errors"], [{"generation": orphan.name, "reason": "previous_backup_manifest_missing"}]
        )
        self.assertEqual(result["rotation_residue"], [])
        self.assertEqual(sentinel.read_bytes(), b"unknown synthetic file")
        self.assertEqual(set(self.root.iterdir()), {orphan, current})

    def test_changed_source_preserves_previous_and_removes_partial(self):
        def mutate(args, **kwargs):
            self.command(args, **kwargs)
            if "--list" in args:
                self.source.write_text("changed during dump")

        with self.assertRaisesRegex(RuntimeError, "source_changed"):
            self.create(mutate)
        self.assertEqual(list(self.root.iterdir()), [self.old])

    def test_backup_binds_current_file_mounts_before_and_after_capture(self):
        uploads = self.base / "uploads"
        photos = self.base / "photos"
        uploads.mkdir()
        photos.mkdir()
        (uploads / "synthetic.txt").write_text("synthetic upload")
        (photos / "synthetic.txt").write_text("synthetic photo")
        mounts = [
            {"Source": str(uploads), "Destination": "/var/data/uploads", "Type": "volume", "RW": False},
            {"Source": str(photos), "Destination": "/var/data/quote-request-vin-photos", "Type": "volume", "RW": True},
        ]
        changed_uploads = [{**mounts[0], "Source": str(self.base / "other-uploads")}, mounts[1]]
        changed_photos = [mounts[0], {**mounts[1], "Source": str(self.base / "other-photos")}]
        db = {"Id": "db", "Image": "db-image", "State": {"Running": True, "Pid": 11, "Health": {"Status": "healthy"}}}
        app = {"Id": "app", "Image": "app-image", "State": {"Pid": 12}}

        def inspected(file_mounts):
            return subprocess.CompletedProcess([], 0, stdout=json.dumps([db, {**app, "Mounts": file_mounts}]).encode())

        for case, first, second in (
            ("changed-uploads", changed_uploads, changed_uploads),
            ("changed-photos", changed_photos, changed_photos),
            ("missing", mounts[:1], mounts[:1]),
            ("duplicate", [*mounts, mounts[0]], [*mounts, mounts[0]]),
            ("changed-during-capture", mounts, changed_photos),
            ("valid", mounts, mounts),
        ):
            with self.subTest(case=case):
                root = self.base / case
                root.mkdir(mode=0o700)
                previous = root / self.old.name
                shutil.copytree(self.old, previous)
                with (
                    patch.object(backup, "SOURCES", {"uploads": uploads, "photos": photos}),
                    patch.object(backup, "container_identity", inspect_containers),
                    patch.object(
                        backup.subprocess, "run", side_effect=[inspected(first), inspected(second)]
                    ) as inspect,
                    patch.object(backup, "quiet_run", side_effect=self.command) as postgres,
                ):
                    if case == "valid":
                        result = backup.create_backup(root, self.base / "mount-test.lock")
                        current = Path(result["backup"])
                        manifest = json.loads((current / "manifest.json").read_text())
                        self.assertEqual(
                            manifest["container_identity"],
                            {
                                "db": {"id": "db", "image": "db-image", "pid": 11},
                                "app": {"id": "app", "image": "app-image", "pid": 12},
                            },
                        )
                        self.assertTrue(
                            {"uploads/synthetic.txt", "photos/synthetic.txt"} <= manifest["source_files"].keys()
                        )
                        self.assertEqual(list(root.iterdir()), [current])
                        self.assertEqual(inspect.call_count, 2)
                    else:
                        with self.assertRaisesRegex(RuntimeError, "store_file_mount_mismatch"):
                            backup.create_backup(root, self.base / "mount-test.lock")
                        self.assertEqual(list(root.iterdir()), [previous])
                        self.assertEqual((previous / "database.dump").read_bytes(), b"old valid backup")
                        if case == "changed-during-capture":
                            self.assertTrue(postgres.called)
                            self.assertEqual(inspect.call_count, 2)
                        else:
                            postgres.assert_not_called()
                            self.assertEqual(inspect.call_count, 1)

    def test_archive_validation_failure_preserves_previous(self):
        def fail(args, **kwargs):
            self.command(args, **kwargs)
            if "--file=/dev/null" in args:
                raise RuntimeError("synthetic corrupt PostgreSQL archive")

        with self.assertRaises(RuntimeError):
            self.create(fail)
        self.assertEqual(list(self.root.iterdir()), [self.old])

    def test_sigterm_cleans_only_its_staging_directory(self):
        probe = '''
import importlib.util
import os
from pathlib import Path
import signal
import sys

script, root, lock, source, child_pid, stage = sys.argv[1:]
spec = importlib.util.spec_from_file_location("store_backup_probe", script)
backup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backup)
backup.SOURCES = {"files/synthetic.txt": Path(source)}
backup.container_identity = lambda: {"db": "synthetic"}
actual_create, actual_uid = backup.create_backup, os.geteuid
actual_run = backup.quiet_run
actual_mkdir = Path.mkdir

def create_backup(*args):
    # The CLI checks root before the test runs with its real filesystem owner.
    os.geteuid = actual_uid
    return actual_create(*args)

def mkdir(path, *args, **kwargs):
    actual_mkdir(path, *args, **kwargs)
    if stage == "creation" and path.name.startswith(".partial-"):
        os.kill(os.getpid(), signal.SIGTERM)

def backup_command(_arguments, *, input_file=None, output_file=None):
    if output_file is None:
        return
    if stage != "dump":
        output_file.write(b"PGDMP synthetic archive")
        return
    code = """
import os, signal, sys, time
from pathlib import Path
Path(sys.argv[1]).write_text(str(os.getpid()))
sys.stdout.buffer.write(b"PGDMP partial archive")
sys.stdout.flush()
os.kill(os.getppid(), signal.SIGTERM)
time.sleep(5)
"""
    actual_run([sys.executable, "-B", "-c", code, child_pid], output_file=output_file)

def interrupt_before_retirement(_root, _current):
    os.kill(os.getpid(), signal.SIGTERM)

backup.create_backup = create_backup
backup.quiet_run = backup_command
backup.retire_previous = interrupt_before_retirement
Path.mkdir = mkdir
os.geteuid = lambda: 0
sys.argv = [script, "--root", root, "--lock", lock]
backup.main()
'''
        child_pid = self.base / "child.pid"
        for stage in ("creation", "dump", "retirement"):
            with self.subTest(stage=stage):
                root = self.base / stage
                root.mkdir(mode=0o700)
                previous = root / self.old.name
                shutil.copytree(self.old, previous)
                existing_partial = root / ".partial-unrelated"
                existing_partial.mkdir()
                result = subprocess.run(
                    [
                        sys.executable,
                        "-B",
                        "-c",
                        probe,
                        str(SPEC.origin),
                        str(root),
                        str(self.base / "backup.lock"),
                        str(self.source),
                        str(child_pid),
                        stage,
                    ],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                self.assertEqual(result.returncode, 128 + signal.SIGTERM, result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertEqual(list(root.glob(".partial-*")), [existing_partial])
                self.assertEqual((previous / "database.dump").read_bytes(), b"old valid backup")
                if stage == "dump":
                    with self.assertRaises(ProcessLookupError):
                        os.kill(int(child_pid.read_text()), 0)
                if stage == "retirement":
                    (current,) = set(root.iterdir()) - {existing_partial, previous}
                    self.assertTrue(json.loads((current / "manifest.json").read_text())["complete"])
                else:
                    self.assertEqual(set(root.iterdir()), {existing_partial, previous})

    def test_staging_name_collision_preserves_existing_directory(self):
        existing = self.root / ".partial-20261120T033000Z-1234"
        existing.mkdir()
        sentinel = existing / "existing"
        sentinel.write_text("preexisting fixture")
        with patch.object(backup, "datetime") as clock, patch.object(backup.os, "getpid", return_value=1234):
            clock.now.return_value = datetime(2026, 11, 20, 3, 30, tzinfo=UTC)
            with self.assertRaises(FileExistsError):
                self.create()
        self.assertEqual(sentinel.read_text(), "preexisting fixture")
        self.assertEqual(set(self.root.iterdir()), {existing, self.old})

    def test_file_archive_checks_content_and_permissions(self):
        records, paths = backup.inventory({"input": self.source})
        archive = self.base / "files.tar"
        backup.archive_files(archive, paths)
        backup.verify_archive(archive, records)
        records["input"]["mode"] = 0o600
        with self.assertRaisesRegex(RuntimeError, "permissions"):
            backup.verify_archive(archive, records)
        records["input"]["mode"] = 0o640
        records["input"]["sha256"] = "0" * 64
        with self.assertRaisesRegex(RuntimeError, "hash"):
            backup.verify_archive(archive, records)

    def test_source_symlink_is_rejected_without_reading_target(self):
        link = self.base / "link"
        link.symlink_to(self.source)
        with self.assertRaisesRegex(RuntimeError, "source_type"):
            backup.inventory({"link": link})

    def test_unknown_directory_is_not_retired(self):
        unrelated = self.root / "unique-work"
        unrelated.mkdir()
        self.create()
        self.assertTrue(unrelated.is_dir())


if __name__ == "__main__":
    unittest.main()
