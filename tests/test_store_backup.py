"""Synthetic boundaries for the one-generation host backup helper."""

import importlib.util
import json
import os
from pathlib import Path
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

    def test_changed_source_preserves_previous_and_removes_partial(self):
        def mutate(args, **kwargs):
            self.command(args, **kwargs)
            if "--list" in args:
                self.source.write_text("changed during dump")

        with self.assertRaisesRegex(RuntimeError, "source_changed"):
            self.create(mutate)
        self.assertEqual(list(self.root.iterdir()), [self.old])

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

def create_backup(*args):
    # The CLI checks root before the test runs with its real filesystem owner.
    os.geteuid = actual_uid
    return actual_create(*args)

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
os.geteuid = lambda: 0
sys.argv = [script, "--root", root, "--lock", lock]
backup.main()
'''
        existing_partial = self.root / ".partial-unrelated"
        existing_partial.mkdir()
        child_pid = self.base / "child.pid"
        for stage in ("dump", "retirement"):
            with self.subTest(stage=stage):
                result = subprocess.run(
                    [
                        sys.executable,
                        "-B",
                        "-c",
                        probe,
                        str(SPEC.origin),
                        str(self.root),
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
                self.assertEqual(list(self.root.glob(".partial-*")), [existing_partial])
                self.assertEqual((self.old / "database.dump").read_bytes(), b"old valid backup")
                if stage == "dump":
                    with self.assertRaises(ProcessLookupError):
                        os.kill(int(child_pid.read_text()), 0)
                    self.assertEqual(set(self.root.iterdir()), {existing_partial, self.old})
                else:
                    (current,) = set(self.root.iterdir()) - {existing_partial, self.old}
                    self.assertTrue(json.loads((current / "manifest.json").read_text())["complete"])

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
