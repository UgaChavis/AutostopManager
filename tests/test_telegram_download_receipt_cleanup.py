from __future__ import annotations

import asyncio
import errno
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from autostop_manager import telegram_bridge as bridge


@pytest.fixture
def download(monkeypatch, tmp_path):
    config = bridge.TelegramConfig(
        123456,
        "0" * 32,
        session_path=tmp_path / "state" / "unused-account",
        state_dir=tmp_path / "state",
        socket_path=tmp_path / "run" / "unused.sock",
    )
    content = b"%PDF-1.4\nSynthetic private download.\n"
    message = SimpleNamespace(
        id=44,
        chat_id=42,
        is_private=True,
        media=SimpleNamespace(),
        file=SimpleNamespace(name="synthetic.pdf", mime_type="application/pdf", size=len(content)),
    )

    async def resolve(_client, peer):
        assert peer == "42"
        return 42, {"id": 42, "kind": "private"}

    class Client:
        downloads = 0

        async def get_messages(self, entity, *, ids):
            assert entity == 42
            assert ids == message.id
            return message

        async def download_media(self, exact_message, *, file):
            assert exact_message is message
            assert file is bytes
            self.downloads += 1
            return content

    monkeypatch.setattr(bridge, "_resolve_peer", resolve)
    client = Client()
    request = {"operation": "download", "peer": "42", "message_id": 44, "mode": "dry_run"}
    dry = asyncio.run(bridge._handle_operation(client, config, request))
    request.update(mode="apply", contract_token=dry["contract_token"], idempotency_key="download-request")

    def apply(key="download-request"):
        return asyncio.run(bridge._handle_operation(client, config, request | {"idempotency_key": key}))

    return SimpleNamespace(
        config=config,
        client=client,
        content=content,
        inbox=config.socket_path.parent / "inbox",
        ledger=config.state_dir / "idempotency.json",
        apply=apply,
    )


def fail_ledger_replace(monkeypatch, download, *, committed=False, unreadable=False, replace_download=False):
    original_replace = bridge.os.replace

    def replace(source, destination):
        if Path(destination) != download.ledger:
            return original_replace(source, destination)
        if replace_download:
            saved_path = next(download.inbox.iterdir())
            previous_inode = saved_path.stat().st_ino
            replacement = download.inbox / ".replacement"
            replacement.write_bytes(download.content)
            replacement.chmod(0o600)
            original_replace(replacement, saved_path)
            assert saved_path.stat().st_ino != previous_inode
        if committed:
            original_replace(source, destination)
            if unreadable:
                download.ledger.chmod(0o000)
        raise OSError(errno.ENOSPC, "synthetic ledger publication failure")

    monkeypatch.setattr(bridge.os, "replace", replace)


def assert_saved(download, saved_path):
    assert saved_path.read_bytes() == download.content
    assert saved_path.stat().st_mode & 0o777 == 0o600
    assert download.inbox.stat().st_mode & 0o777 == 0o700
    assert not list(download.inbox.glob(".*"))
    assert not list(download.ledger.parent.glob(".idempotency.json.*.tmp"))


def test_new_download_is_removed_when_ledger_did_not_commit(monkeypatch, download):
    with monkeypatch.context() as fault:
        fail_ledger_replace(fault, download)
        with pytest.raises(OSError) as error:
            download.apply()
    assert error.value.errno == errno.ENOSPC
    assert list(download.inbox.iterdir()) == []
    assert not download.ledger.exists()
    assert not list(download.ledger.parent.glob(".idempotency.json.*.tmp"))

    applied = download.apply()
    saved_path = Path(applied["saved_path"])
    assert applied["verified"] is True
    assert applied["replayed"] is False
    assert applied["sha256"] == hashlib.sha256(download.content).hexdigest()
    assert_saved(download, saved_path)
    removed = asyncio.run(
        bridge._handle_operation(
            download.client, download.config, {"operation": "discard_download", "path": str(saved_path)}
        )
    )
    assert removed["removed"] is True
    assert not saved_path.exists()


@pytest.mark.parametrize("existing_receipt", [False, True], ids=["preexisting-file", "another-receipt"])
def test_failed_receipt_preserves_existing_download(monkeypatch, download, existing_receipt):
    if existing_receipt:
        saved_path = Path(download.apply("earlier-download")["saved_path"])
        previous_ledger = download.ledger.read_bytes()
    else:
        saved_path = bridge._save_private_download(
            download.content, message_id=44, suffix=".pdf", inbox_dir=download.inbox
        )[0]
        previous_ledger = None
    original_inode = saved_path.stat().st_ino

    with monkeypatch.context() as fault:
        fail_ledger_replace(fault, download)
        with pytest.raises(OSError) as error:
            download.apply()
    assert error.value.errno == errno.ENOSPC
    assert_saved(download, saved_path)
    assert saved_path.stat().st_ino == original_inode
    if previous_ledger is None:
        assert not download.ledger.exists()
    else:
        assert download.ledger.read_bytes() == previous_ledger
        downloads = download.client.downloads
        assert download.apply("earlier-download")["replayed"] is True
        assert download.client.downloads == downloads


@pytest.mark.parametrize("unreadable", [False, True], ids=["committed", "unknown-readback"])
def test_receipt_committed_before_error_preserves_download_and_replays(monkeypatch, download, unreadable):
    with monkeypatch.context() as fault:
        fail_ledger_replace(fault, download, committed=True, unreadable=unreadable)
        with pytest.raises(OSError) as error:
            download.apply()
    assert error.value.errno == errno.ENOSPC
    download.ledger.chmod(0o600)
    receipts = json.loads(download.ledger.read_bytes())
    assert set(receipts) == {"download-request"}
    receipt = receipts["download-request"]
    saved_path = Path(receipt["saved_path"])
    assert receipt["sha256"] == hashlib.sha256(download.content).hexdigest()
    assert_saved(download, saved_path)
    replayed = download.apply()
    assert replayed["replayed"] is True
    assert replayed["saved_path"] == str(saved_path)
    assert download.client.downloads == 1


def test_failed_receipt_preserves_a_replacement_inode(monkeypatch, download):
    with monkeypatch.context() as fault:
        fail_ledger_replace(fault, download, replace_download=True)
        with pytest.raises(OSError) as error:
            download.apply()
    assert error.value.errno == errno.ENOSPC
    assert not download.ledger.exists()
    saved_path = next(download.inbox.iterdir())
    assert_saved(download, saved_path)
