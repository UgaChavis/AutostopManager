import importlib.util
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import tarfile
import unittest
from unittest import mock

from cryptography.fernet import Fernet, InvalidToken


def load(filename, name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).parents[1] / "scripts" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


snapshot = load("crm-snapshot.py", "crm_snapshot")
restore = load("check-crm-snapshot-restore.py", "crm_restore")


class CrmSnapshotTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.source = self.base / "source"
        self.source.mkdir()
        self.stage = self.base / "staging"
        self.stage.mkdir(mode=0o700)
        self.key = Fernet.generate_key()
        self.runtime = {
            "schema": 1,
            "environment": {"MINIMAL_KANBAN_MCP_BEARER_TOKEN": "synthetic-bearer", "AUTOSTOP_MCP_OAUTH_ENABLED": "1"},
        }
        for name in ("state.json", "settings.json", "users.json"):
            (self.source / name).write_text(json.dumps({"synthetic": 1}))
        (self.source / "mcp-oauth-state.json").write_bytes(Fernet(self.key).encrypt(b'{"clients":{}}'))
        (self.source / "attachments").mkdir()
        (self.source / "attachments" / "example").write_bytes(b"synthetic attachment")
        (self.source / "logs").mkdir()
        (self.source / "logs" / "technical").write_text("skip")
        self.db = sqlite3.connect(self.source / "change_feed.sqlite3")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("CREATE TABLE sample (value TEXT)")
        self.db.execute("INSERT INTO sample VALUES ('synthetic')")
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_real_archive_and_isolated_restore_with_key_and_wal(self):
        path = self.stage / "snapshot.tar.gz"
        snapshot.create_snapshot(self.source, path, "a" * 40, self.key, "sha256:" + "c" * 64, self.runtime)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        destination = self.base / "restore"
        destination.mkdir(mode=0o700)
        self.assertEqual(restore.check_archive(path, destination), "a" * 40)
        self.assertEqual((destination / "oauth-state.key").read_bytes(), self.key)
        self.assertEqual(json.loads((destination / "runtime-config.json").read_text()), self.runtime)
        self.assertEqual((destination / "runtime-config.json").stat().st_mode & 0o777, 0o600)
        self.assertEqual((destination / "crm" / "attachments" / "example").read_bytes(), b"synthetic attachment")
        self.assertFalse((destination / "crm" / "logs").exists())
        self.assertFalse((destination / "crm" / "change_feed.sqlite3-wal").exists())
        with sqlite3.connect(destination / "crm" / "change_feed.sqlite3") as db:
            self.assertEqual(db.execute("SELECT value FROM sample").fetchone(), ("synthetic",))

    def test_cold_wal_snapshot_does_not_create_source_sidecars(self):
        self.db.close()
        self.db = sqlite3.connect(":memory:")  # tearDown closes this unrelated connection
        feed = self.source / "change_feed.sqlite3"
        self.assertFalse(Path(str(feed) + "-wal").exists())
        self.assertFalse(Path(str(feed) + "-shm").exists())
        root_before = self.source.stat()
        path = self.stage / "snapshot.tar.gz"
        snapshot.create_snapshot(self.source, path, "a" * 40, self.key, "sha256:" + "c" * 64, self.runtime)
        self.assertEqual(self.source.stat().st_mtime_ns, root_before.st_mtime_ns)
        self.assertFalse(Path(str(feed) + "-wal").exists())
        self.assertFalse(Path(str(feed) + "-shm").exists())
        destination = self.base / "restore"
        destination.mkdir(mode=0o700)
        restore.check_archive(path, destination)
        with sqlite3.connect(destination / "crm" / "change_feed.sqlite3") as db:
            self.assertEqual(db.execute("SELECT value FROM sample").fetchone(), ("synthetic",))

    def test_replaced_feed_symlink_is_rejected_before_sqlite_reads_it(self):
        path = self.stage / "snapshot.tar.gz"
        feed = self.source / "change_feed.sqlite3"
        saved = self.base / "saved-feed.sqlite3"
        original = snapshot._sqlite_copy

        def replace_feed(*args):
            feed.rename(saved)
            feed.symlink_to(self.source / "state.json")
            try:
                with mock.patch.object(snapshot.sqlite3, "connect") as connect:
                    with self.assertRaises(OSError):
                        original(*args)
                    connect.assert_not_called()
                raise ValueError("source changed")
            finally:
                feed.unlink()
                saved.rename(feed)

        with mock.patch.object(snapshot, "_sqlite_copy", side_effect=replace_feed):
            with self.assertRaisesRegex(ValueError, "source changed"):
                snapshot.create_snapshot(self.source, path, "a" * 40, self.key, "sha256:" + "c" * 64, self.runtime)
        self.assertFalse(path.exists())

    def test_wal_changed_after_copy_rejects_archive(self):
        path = self.stage / "snapshot.tar.gz"
        wal = self.source / "change_feed.sqlite3-wal"
        original = tarfile.TarFile.addfile

        def change_wal(archive, entry, source=None):
            result = original(archive, entry, source)
            if entry.name == "crm/state.json":
                metadata = wal.stat()
                os.utime(wal, ns=(metadata.st_atime_ns, metadata.st_mtime_ns + 1_000_000_000))
            return result

        with mock.patch.object(tarfile.TarFile, "addfile", new=change_wal):
            with self.assertRaisesRegex(ValueError, "tree changed"):
                snapshot.create_snapshot(self.source, path, "a" * 40, self.key, "sha256:" + "c" * 64, self.runtime)
        self.assertFalse(path.exists())

    def test_wal_appearing_during_snapshot_rejects_archive(self):
        self.db.close()
        self.db = sqlite3.connect(":memory:")
        path = self.stage / "snapshot.tar.gz"
        wal = self.source / "change_feed.sqlite3-wal"
        self.assertFalse(wal.exists())
        original = snapshot._sqlite_copy

        def add_wal(*args):
            wal.write_bytes(b"synthetic WAL")
            return original(*args)

        with mock.patch.object(snapshot, "_sqlite_copy", side_effect=add_wal):
            with self.assertRaisesRegex(ValueError, "tree changed"):
                snapshot.create_snapshot(self.source, path, "a" * 40, self.key, "sha256:" + "c" * 64, self.runtime)
        self.assertFalse(path.exists())

    def test_replaced_wal_is_rejected_before_copy(self):
        path = self.stage / "snapshot.tar.gz"
        wal = self.source / "change_feed.sqlite3-wal"
        saved = self.base / "saved-wal"
        original = snapshot._sqlite_copy

        def replace_wal(*args):
            wal.rename(saved)
            wal.write_bytes(saved.read_bytes())
            try:
                return original(*args)
            finally:
                wal.unlink()
                saved.rename(wal)

        with mock.patch.object(snapshot, "_sqlite_copy", side_effect=replace_wal):
            with self.assertRaisesRegex(ValueError, "source changed"):
                snapshot.create_snapshot(self.source, path, "a" * 40, self.key, "sha256:" + "c" * 64, self.runtime)
        self.assertFalse(path.exists())

    def test_wrong_key_and_symlink_do_not_publish(self):
        path = self.stage / "snapshot.tar.gz"
        with self.assertRaises(InvalidToken):
            snapshot.create_snapshot(
                self.source, path, "a" * 40, Fernet.generate_key(), "sha256:" + "c" * 64, self.runtime
            )
        self.assertFalse(path.exists())
        (self.source / "attachments" / "bad").symlink_to(self.source / "state.json")
        with self.assertRaises(ValueError):
            snapshot.create_snapshot(self.source, path, "a" * 40, self.key, "sha256:" + "c" * 64, self.runtime)
        self.assertFalse(path.exists())

    def test_key_read_only_from_exact_stopped_container(self):
        ident = "b" * 64
        item = {
            "Id": ident,
            "Image": "sha256:" + "c" * 64,
            "State": {"Running": False, "ExitCode": 0, "OOMKilled": False},
            "Mounts": [{"Destination": "/home/autostop/.minimal-kanban", "Source": str(self.source), "RW": True}],
            "Config": {
                "Env": [
                    "AUTOSTOP_MCP_OAUTH_STATE_KEY=" + self.key.decode(),
                    "MINIMAL_KANBAN_MCP_BEARER_TOKEN=synthetic-bearer",
                    "AUTOSTOP_MCP_OAUTH_ENABLED=1",
                    "OPENAI_API_KEY=synthetic-not-in-backup",
                    "AUTOSTOP_STORE_MANAGE_TOKEN=synthetic-not-in-backup",
                ]
            },
        }
        result = mock.Mock(stdout=json.dumps([item]).encode())
        image = mock.Mock(
            stdout=json.dumps(
                [{"Id": "sha256:" + "c" * 64, "Config": {"Labels": {"org.opencontainers.image.revision": "a" * 40}}}]
            ).encode()
        )
        with mock.patch.object(snapshot.subprocess, "run", side_effect=[result, image, result]):
            self.assertEqual(
                snapshot.container_context(ident, self.source, "a" * 40), (self.key, "sha256:" + "c" * 64, self.runtime)
            )
            item["State"]["Running"] = True
            result.stdout = json.dumps([item]).encode()
            with self.assertRaises(ValueError):
                snapshot.container_context(ident, self.source, "a" * 40)

    def test_corrupted_archive_fails_integrity_check(self):
        path = self.stage / "snapshot.tar.gz"
        changed = self.stage / "modified.tar.gz"
        snapshot.create_snapshot(self.source, path, "a" * 40, self.key, "sha256:" + "c" * 64, self.runtime)
        with tarfile.open(path, "r:gz") as source, tarfile.open(changed, "w:gz") as target:
            for entry in source:
                if entry.isfile():
                    data = source.extractfile(entry).read()
                    if entry.name == "crm/attachments/example":
                        data = b"x" * len(data)
                    target.addfile(entry, io.BytesIO(data))
                else:
                    target.addfile(entry)
        destination = self.base / "restore"
        destination.mkdir(mode=0o700)
        with self.assertRaises(ValueError):
            restore.check_archive(changed, destination)

    def test_sqlite_attachment_is_preserved_byte_for_byte(self):
        attachment = self.source / "attachments" / "database-attachment"
        with sqlite3.connect(attachment) as database:
            database.execute("CREATE TABLE synthetic (value TEXT)")
            database.execute("INSERT INTO synthetic VALUES ('original')")
        original = attachment.read_bytes()
        path = self.stage / "snapshot.tar.gz"
        snapshot.create_snapshot(self.source, path, "a" * 40, self.key, "sha256:" + "c" * 64, self.runtime)
        destination = self.base / "restore"
        destination.mkdir(mode=0o700)
        restore.check_archive(path, destination)
        self.assertEqual((destination / "crm" / "attachments" / "database-attachment").read_bytes(), original)

    def test_unknown_system_database_fails_closed(self):
        with sqlite3.connect(self.source / "unknown.sqlite3") as database:
            database.execute("CREATE TABLE synthetic (value TEXT)")
        path = self.stage / "snapshot.tar.gz"
        with self.assertRaises(ValueError):
            snapshot.create_snapshot(self.source, path, "a" * 40, self.key, "sha256:" + "c" * 64, self.runtime)
        self.assertFalse(path.exists())

    def test_new_attachment_after_walk_rejects_archive(self):
        path = self.stage / "snapshot.tar.gz"
        original = tarfile.TarFile.addfile

        def change_tree(archive, entry, source=None):
            result = original(archive, entry, source)
            if entry.name == "crm/state.json":
                (self.source / "attachments" / "late-file").write_bytes(b"synthetic")
            return result

        with mock.patch.object(tarfile.TarFile, "addfile", new=change_tree):
            with self.assertRaisesRegex(ValueError, "tree changed"):
                snapshot.create_snapshot(self.source, path, "a" * 40, self.key, "sha256:" + "c" * 64, self.runtime)
        self.assertFalse(path.exists())

    def test_replaced_file_after_read_rejects_archive(self):
        path = self.stage / "snapshot.tar.gz"
        original = tarfile.TarFile.addfile

        def replace_state(archive, entry, source=None):
            result = original(archive, entry, source)
            if entry.name == "crm/users.json":
                replacement = self.source / "temporary-state"
                replacement.write_text('{"synthetic": 1}')
                os.replace(replacement, self.source / "state.json")
            return result

        with mock.patch.object(tarfile.TarFile, "addfile", new=replace_state):
            with self.assertRaisesRegex(ValueError, "tree changed"):
                snapshot.create_snapshot(self.source, path, "a" * 40, self.key, "sha256:" + "c" * 64, self.runtime)
        self.assertFalse(path.exists())

    def test_runtime_config_allowlist_excludes_unrelated_secrets_and_rejects_duplicates(self):
        config = snapshot.runtime_config_from_environment(
            [
                "MINIMAL_KANBAN_MCP_BEARER_TOKEN=synthetic-bearer",
                "MINIMAL_KANBAN_API_BEARER_TOKEN=",
                "OPENAI_API_KEY=not-saved",
                "AUTOSTOP_STORE_MANAGE_TOKEN=not-saved",
            ]
        )
        self.assertEqual(
            config["environment"],
            {"MINIMAL_KANBAN_MCP_BEARER_TOKEN": "synthetic-bearer", "MINIMAL_KANBAN_API_BEARER_TOKEN": ""},
        )
        with self.assertRaisesRegex(ValueError, "duplicate"):
            snapshot.runtime_config_from_environment(
                ["MINIMAL_KANBAN_MCP_BEARER_TOKEN=a", "MINIMAL_KANBAN_MCP_BEARER_TOKEN=b"]
            )
        with self.assertRaisesRegex(ValueError, "runtime configuration"):
            snapshot.validate_runtime_config({"schema": 1, "environment": {"OPENAI_API_KEY": "not-allowed"}})

    def test_runtime_config_is_covered_by_archive_hash(self):
        path = self.stage / "snapshot.tar.gz"
        changed = self.stage / "modified.tar.gz"
        snapshot.create_snapshot(self.source, path, "a" * 40, self.key, "sha256:" + "c" * 64, self.runtime)
        with tarfile.open(path, "r:gz") as source, tarfile.open(changed, "w:gz") as target:
            for entry in source:
                if entry.isfile():
                    data = source.extractfile(entry).read()
                    if entry.name == "runtime-config.json":
                        data = data.replace(b"synthetic-bearer", b"synthetic-change")
                    target.addfile(entry, io.BytesIO(data))
                else:
                    target.addfile(entry)
        destination = self.base / "restore"
        destination.mkdir(mode=0o700)
        with self.assertRaisesRegex(ValueError, "integrity"):
            restore.check_archive(changed, destination)

    def test_legacy_archive_remains_a_data_and_key_restore_only(self):
        path = self.stage / "snapshot.tar.gz"
        legacy = self.stage / "legacy.tar.gz"
        snapshot.create_snapshot(self.source, path, "a" * 40, self.key, "sha256:" + "c" * 64, self.runtime)
        digest = hashlib.sha256()
        with tarfile.open(path, "r:gz") as source, tarfile.open(legacy, "w:gz") as target:
            for entry in source:
                if entry.name == "runtime-config.json":
                    continue
                data = source.extractfile(entry).read() if entry.isfile() else None
                if entry.name == "manifest.json":
                    value = json.loads(data)
                    value.update(schema=1, tree_sha256=digest.hexdigest())
                    data = json.dumps(value).encode()
                    entry.size = len(data)
                else:
                    snapshot._record(digest, entry, hashlib.sha256(data).hexdigest() if data is not None else "")
                target.addfile(entry, io.BytesIO(data) if data is not None else None)
        destination = self.base / "restore"
        destination.mkdir(mode=0o700)
        self.assertEqual(restore.check_archive(legacy, destination), "a" * 40)
        self.assertFalse((destination / "runtime-config.json").exists())


if __name__ == "__main__":
    unittest.main()
