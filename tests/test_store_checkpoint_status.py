"""Checkpoint status must inspect synthetic state without initializing it."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from autostop_manager import cli, storage
from autostop_manager.storage import StoreState


def _status(monkeypatch, capsys, path: Path, stream: str = "store_digest"):
    monkeypatch.setattr(cli, "StoreState", lambda: StoreState(path))
    code = cli.main(["store-checkpoint-status", "--stream", stream])
    return code, json.loads(capsys.readouterr().out)


def _failure(stream: str, category: str) -> dict:
    return {
        "ok": False,
        "read_only": True,
        "stream": stream,
        "error": f"store_checkpoint_{category}_unavailable",
    }


def _schema(path: Path):
    with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)) as conn:
        return conn.execute("PRAGMA schema_version").fetchone(), conn.execute(
            "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
        ).fetchall()


@pytest.mark.parametrize("missing_parent", [False, True])
@pytest.mark.parametrize("stream", ["store_digest", "store_bootstrap"])
def test_status_missing_database_does_not_create_database_or_parent(
    monkeypatch, capsys, tmp_path, missing_parent, stream
):
    path = (tmp_path / "missing-parent" if missing_parent else tmp_path) / "checkpoint.sqlite3"
    before = set(tmp_path.iterdir())

    code, result = _status(monkeypatch, capsys, path, stream)

    assert code == 1
    assert result == _failure(stream, "database")
    assert set(tmp_path.iterdir()) == before
    assert not path.exists()


@pytest.mark.parametrize("has_row", [False, True])
@pytest.mark.parametrize("stream", ["store_digest", "store_bootstrap"])
def test_status_legacy_schema_is_not_migrated(monkeypatch, capsys, tmp_path, has_row, stream):
    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE store_checkpoints (
                stream TEXT PRIMARY KEY, cursor TEXT, last_success_at TEXT,
                compact_refs_json TEXT NOT NULL DEFAULT '[]',
                last_attempt_status TEXT NOT NULL DEFAULT 'never',
                last_error_code TEXT NOT NULL DEFAULT '',
                state_version INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL
            )
            """
        )
        if has_row:
            conn.execute(
                "INSERT INTO store_checkpoints (stream, cursor, updated_at) VALUES (?, ?, ?)",
                (stream, "synthetic-legacy-cursor", "2026-10-11T00:00:00Z"),
            )
    before = path.read_bytes(), _schema(path), set(tmp_path.iterdir())

    code, result = _status(monkeypatch, capsys, path, stream)

    assert code == 1
    assert result == _failure(stream, "schema")
    assert (path.read_bytes(), _schema(path), set(tmp_path.iterdir())) == before


@pytest.mark.parametrize("schema", ["empty", "unrelated", "incomplete"])
def test_status_missing_or_incomplete_checkpoint_schema_fails_closed(monkeypatch, capsys, tmp_path, schema):
    path = tmp_path / "incomplete.sqlite3"
    if schema == "incomplete":
        StoreState(path).initialize()
        with sqlite3.connect(path) as conn:
            conn.execute("ALTER TABLE store_checkpoints DROP COLUMN traversal_snapshot_at")
    else:
        with sqlite3.connect(path) as conn:
            if schema == "unrelated":
                conn.execute("CREATE TABLE unrelated (value TEXT)")
    before = path.read_bytes(), _schema(path), set(tmp_path.iterdir())

    code, result = _status(monkeypatch, capsys, path)

    assert code == 1
    assert result == _failure("store_digest", "schema")
    assert (path.read_bytes(), _schema(path), set(tmp_path.iterdir())) == before


def test_status_corrupt_database_has_bounded_error_and_does_not_repair(monkeypatch, capsys, tmp_path):
    path = tmp_path / "corrupt.sqlite3"
    path.write_bytes(b"synthetic-not-a-database")
    before = path.read_bytes(), set(tmp_path.iterdir())

    code, result = _status(monkeypatch, capsys, path)

    assert code == 1
    assert result == _failure("store_digest", "state")
    assert (path.read_bytes(), set(tmp_path.iterdir())) == before


def test_status_unreadable_database_has_bounded_error(monkeypatch, capsys, tmp_path):
    path = tmp_path / "checkpoint.sqlite3"
    StoreState(path).initialize()
    before = path.read_bytes()

    def unavailable(*args, **kwargs):
        raise sqlite3.OperationalError("PRIVATE filesystem diagnostic")

    monkeypatch.setattr(storage.sqlite3, "connect", unavailable)
    code, result = _status(monkeypatch, capsys, path)

    assert code == 1
    assert result == _failure("store_digest", "database")
    assert path.read_bytes() == before


def test_status_read_failure_is_bounded_and_closes_connection(monkeypatch, capsys, tmp_path):
    path = tmp_path / "checkpoint.sqlite3"
    StoreState(path).initialize()
    before = path.read_bytes()
    real_connect = sqlite3.connect
    opened = []

    def deny_reads(action, *args):
        return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_SELECT else sqlite3.SQLITE_OK

    def connect(*args, **kwargs):
        connection = real_connect(*args, **kwargs)
        connection.set_authorizer(deny_reads)
        opened.append(connection)
        return connection

    monkeypatch.setattr(storage.sqlite3, "connect", connect)
    code, result = _status(monkeypatch, capsys, path)

    assert code == 1
    assert result == _failure("store_digest", "state")
    assert path.read_bytes() == before
    assert len(opened) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        opened[0].execute("SELECT 1")


@pytest.mark.parametrize("stream", ["store_digest", "store_bootstrap"])
@pytest.mark.parametrize("has_row", [False, True])
def test_status_current_schema_preserves_successful_result_without_writer_path(
    monkeypatch, capsys, tmp_path, stream, has_row
):
    path = tmp_path / "checkpoint.sqlite3"
    store = StoreState(path)
    store.initialize()
    if has_row:
        with store.connect() as conn:
            conn.execute(
                """
                INSERT INTO store_checkpoints (
                    stream, cursor, compact_refs_json, traversal_refs_json,
                    traversal_baseline, pending_refs_json, pending_page_refs_json,
                    pending_baseline, pending_page_has_more, last_ack_was_final,
                    state_version, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    stream,
                    "synthetic-current-cursor",
                    '[{"entity":"store_order","id":"synthetic-1"}]',
                    '[{"entity":"store_order","id":"synthetic-2"}]',
                    1,
                    '[{"entity":"store_order","id":"synthetic-3"}]',
                    '[{"entity":"store_order","id":"synthetic-4"}]',
                    1,
                    1,
                    1,
                    7,
                    "2026-10-11T00:00:00Z",
                ),
            )
    expected = store.get_store_checkpoint(stream)
    before = path.read_bytes(), _schema(path), set(tmp_path.iterdir())

    def must_not_write(*args, **kwargs):
        pytest.fail("status must not use initialization or the writer connection")

    monkeypatch.setattr(StoreState, "initialize", must_not_write)
    monkeypatch.setattr(StoreState, "connect", must_not_write)
    code, result = _status(monkeypatch, capsys, path, stream)

    assert code == 0
    assert result == expected
    assert (path.read_bytes(), _schema(path), set(tmp_path.iterdir())) == before


def test_status_reads_latest_committed_wal_value_without_changing_main_database(monkeypatch, capsys, tmp_path):
    path = tmp_path / "checkpoint.sqlite3"
    StoreState(path).initialize()
    writer = sqlite3.connect(path)
    try:
        assert writer.execute("PRAGMA journal_mode = WAL").fetchone()[0] == "wal"
        writer.execute("PRAGMA wal_autocheckpoint = 0")
        writer.execute(
            "INSERT INTO store_checkpoints (stream, cursor, state_version, updated_at) VALUES (?, ?, ?, ?)",
            ("store_digest", "wal-cursor", 9, "2026-10-11T00:00:00Z"),
        )
        writer.commit()
        assert Path(f"{path}-wal").is_file()
        before = path.read_bytes()

        code, result = _status(monkeypatch, capsys, path)

        assert code == 0
        assert result["cursor"] == "wal-cursor"
        assert result["state_version"] == 9
        assert path.read_bytes() == before
    finally:
        writer.close()


def test_status_accepts_read_only_database(monkeypatch, capsys, tmp_path):
    path = tmp_path / "checkpoint.sqlite3"
    StoreState(path).initialize()
    before = path.read_bytes()
    path.chmod(0o444)
    tmp_path.chmod(0o555)
    try:
        code, result = _status(monkeypatch, capsys, path)
        assert code == 0
        assert result["exists"] is False
        assert path.read_bytes() == before
    finally:
        tmp_path.chmod(0o755)
        path.chmod(0o644)


def test_status_cli_rejects_unknown_stream_before_constructing_store(monkeypatch, capsys):
    monkeypatch.setattr(cli, "StoreState", lambda: pytest.fail("invalid stream must not open state"))
    with pytest.raises(SystemExit) as error:
        cli.main(["store-checkpoint-status", "--stream", "unknown"])
    assert error.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


@pytest.mark.parametrize("stream", ["unknown", "", None, 17])
def test_status_reader_rejects_invalid_stream_before_resolving_or_opening_database(monkeypatch, stream):
    def must_not_read(*args, **kwargs):
        pytest.fail("invalid stream must not inspect or open the database")

    monkeypatch.setattr(StoreState, "path", property(must_not_read))
    monkeypatch.setattr(storage.sqlite3, "connect", must_not_read)

    assert StoreState().read_store_checkpoint(stream) == {"ok": False, "error": "store_checkpoint_stream_invalid"}


def test_status_schema_and_row_use_one_snapshot_during_concurrent_wal_commit(monkeypatch, capsys, tmp_path):
    path = tmp_path / "checkpoint.sqlite3"
    StoreState(path).initialize()
    real_connect = sqlite3.connect
    writer = real_connect(path)
    try:
        writer.execute("PRAGMA journal_mode = WAL")
        writer.execute("PRAGMA wal_autocheckpoint = 0")
        writer.execute(
            "INSERT INTO store_checkpoints (stream, cursor, state_version, updated_at) VALUES (?, ?, ?, ?)",
            ("store_digest", "before-concurrent-commit", 5, "2026-10-11T00:00:00Z"),
        )
        writer.commit()

        class SchemaCursor:
            def __init__(self, cursor):
                self.cursor = cursor

            def __iter__(self):
                rows = self.cursor.fetchall()
                writer.execute(
                    "UPDATE store_checkpoints SET cursor = ?, state_version = ? WHERE stream = ?",
                    ("after-concurrent-commit", 6, "store_digest"),
                )
                writer.commit()
                return iter(rows)

        class Reader:
            def __init__(self, connection):
                self.connection = connection

            @property
            def row_factory(self):
                return self.connection.row_factory

            @row_factory.setter
            def row_factory(self, value):
                self.connection.row_factory = value

            def execute(self, statement, *args):
                cursor = self.connection.execute(statement, *args)
                if statement == "PRAGMA table_info(store_checkpoints)":
                    return SchemaCursor(cursor)
                return cursor

            def close(self):
                self.connection.close()

        def read_only_connect(database, **kwargs):
            assert "mode=ro" in database
            assert "immutable" not in database
            connection = real_connect(database, **kwargs)
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                connection.execute("DELETE FROM store_checkpoints")
            connection.rollback()
            return Reader(connection)

        monkeypatch.setattr(storage.sqlite3, "connect", read_only_connect)
        code, result = _status(monkeypatch, capsys, path)

        assert code == 0
        assert result["cursor"] == "before-concurrent-commit"
        assert result["state_version"] == 5
        assert writer.execute("SELECT cursor, state_version FROM store_checkpoints").fetchone() == (
            "after-concurrent-commit",
            6,
        )
    finally:
        writer.close()
