import importlib.util
import io
import json
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
        snapshot.create_snapshot(self.source, path, "a" * 40, self.key, "sha256:" + "c" * 64)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        destination = self.base / "restore"
        destination.mkdir(mode=0o700)
        self.assertEqual(restore.check_archive(path, destination), "a" * 40)
        self.assertEqual((destination / "oauth-state.key").read_bytes(), self.key)
        self.assertEqual((destination / "crm" / "attachments" / "example").read_bytes(), b"synthetic attachment")
        self.assertFalse((destination / "crm" / "logs").exists())
        self.assertFalse((destination / "crm" / "change_feed.sqlite3-wal").exists())
        with sqlite3.connect(destination / "crm" / "change_feed.sqlite3") as db:
            self.assertEqual(db.execute("SELECT value FROM sample").fetchone(), ("synthetic",))

    def test_wrong_key_and_symlink_do_not_publish(self):
        path = self.stage / "snapshot.tar.gz"
        with self.assertRaises(InvalidToken):
            snapshot.create_snapshot(self.source, path, "a" * 40, Fernet.generate_key(), "sha256:" + "c" * 64)
        self.assertFalse(path.exists())
        (self.source / "attachments" / "bad").symlink_to(self.source / "state.json")
        with self.assertRaises(ValueError):
            snapshot.create_snapshot(self.source, path, "a" * 40, self.key, "sha256:" + "c" * 64)
        self.assertFalse(path.exists())

    def test_key_read_only_from_exact_stopped_container(self):
        ident = "b" * 64
        item = {
            "Id": ident,
            "Image": "sha256:" + "c" * 64,
            "State": {"Running": False, "ExitCode": 0, "OOMKilled": False},
            "Mounts": [{"Destination": "/home/autostop/.minimal-kanban", "Source": str(self.source), "RW": True}],
            "Config": {"Env": ["AUTOSTOP_MCP_OAUTH_STATE_KEY=" + self.key.decode()]},
        }
        result = mock.Mock(stdout=json.dumps([item]).encode())
        image = mock.Mock(
            stdout=json.dumps(
                [{"Id": "sha256:" + "c" * 64, "Config": {"Labels": {"org.opencontainers.image.revision": "a" * 40}}}]
            ).encode()
        )
        with mock.patch.object(snapshot.subprocess, "run", side_effect=[result, image, result]):
            self.assertEqual(snapshot.container_context(ident, self.source, "a" * 40), (self.key, "sha256:" + "c" * 64))
            item["State"]["Running"] = True
            result.stdout = json.dumps([item]).encode()
            with self.assertRaises(ValueError):
                snapshot.container_context(ident, self.source, "a" * 40)

    def test_corrupted_archive_fails_integrity_check(self):
        path = self.stage / "snapshot.tar.gz"
        changed = self.stage / "modified.tar.gz"
        snapshot.create_snapshot(self.source, path, "a" * 40, self.key, "sha256:" + "c" * 64)
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


if __name__ == "__main__":
    unittest.main()
