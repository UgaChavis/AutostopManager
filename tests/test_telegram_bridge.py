from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from autostop_manager import telegram_bridge
from autostop_manager.telegram_bridge import (
    BridgeError,
    TelegramConfig,
    _ensure_private_key,
    _read_private_download,
    _read_one_time_password,
    _requires_mutation_lock,
    _save_qr,
    build_parser,
    issue_download_contract,
    issue_send_contract,
    issue_photo_contract,
    redact_sensitive_message_text,
    validate_download_request,
    validate_photo_file,
    validate_photo_request,
    validate_send_request,
    verify_download_contract,
    verify_photo_contract,
    verify_send_contract,
)


ROOT = Path(__file__).resolve().parents[1]


def test_telegram_service_uses_dedicated_immutable_telegram_release() -> None:
    service = (ROOT / "deploy/systemd/autostop-telegram.service").read_text(encoding="utf-8")

    assert "WorkingDirectory=/opt/autostop-telegram-releases/current" in service
    assert "Environment=PYTHONPATH=/opt/autostop-telegram-releases/current" in service
    assert "/opt/AutostopManager" not in service


def test_dedicated_telegram_deploy_script_is_syntax_valid_and_scoped() -> None:
    script = ROOT / "scripts/deploy_telegram_bridge.sh"
    completed = subprocess.run(["bash", "-n", str(script)], check=False, capture_output=True, text=True)

    assert completed.returncode == 0
    text = script.read_text(encoding="utf-8")
    assert "origin/${BRANCH}" in text
    assert 'git -C "${SOURCE_DIR}" archive' in text
    assert "autostop-telegram.service" in text
    assert '"${previous_release}/deploy/systemd/autostop-telegram.service"' in text
    assert "docker" not in text


def test_private_download_reader_handles_short_system_reads(monkeypatch, tmp_path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir(mode=0o700)
    content = b"%PDF-1.7\ncomplete-content"
    download = inbox / "42-example.pdf"
    download.write_bytes(content)
    download.chmod(0o600)
    real_read = telegram_bridge.os.read

    monkeypatch.setattr(
        telegram_bridge.os,
        "read",
        lambda descriptor, count: real_read(descriptor, min(count, 3)),
    )

    assert _read_private_download(download, inbox_dir=inbox) == content


def test_credentials_require_private_permissions_and_valid_values(tmp_path) -> None:
    credentials = tmp_path / "credentials"
    credentials.write_text(
        "TELEGRAM_API_ID=12345678\nTELEGRAM_API_HASH=0123456789abcdef0123456789abcdef\n",
        encoding="ascii",
    )
    os.chmod(credentials, 0o600)

    config = TelegramConfig.load(credentials)

    assert config.api_id == 12345678
    assert config.api_hash.endswith("cdef")


def test_credentials_fail_closed_when_group_readable(tmp_path) -> None:
    credentials = tmp_path / "credentials"
    credentials.write_text(
        "TELEGRAM_API_ID=12345678\nTELEGRAM_API_HASH=0123456789abcdef0123456789abcdef\n",
        encoding="ascii",
    )
    os.chmod(credentials, 0o640)

    with pytest.raises(BridgeError, match="credentials_permissions_too_open"):
        TelegramConfig.load(credentials)


def test_credentials_reject_symlink_without_reading_target(tmp_path) -> None:
    target = tmp_path / "real-credentials"
    target.write_text(
        "TELEGRAM_API_ID=12345678\nTELEGRAM_API_HASH=0123456789abcdef0123456789abcdef\n",
        encoding="ascii",
    )
    target.chmod(0o600)
    credentials = tmp_path / "credentials"
    credentials.symlink_to(target.name)

    with pytest.raises(BridgeError, match="credentials_unavailable"):
        TelegramConfig.load(credentials)


def test_contract_key_is_exact_private_regular_file(tmp_path) -> None:
    key_path = tmp_path / "state" / "contract.key"

    key = _ensure_private_key(key_path)

    assert len(key) == 32
    assert key_path.stat().st_mode & 0o777 == 0o600
    assert _ensure_private_key(key_path) == key


def test_runtime_paths_support_an_isolated_second_account(tmp_path) -> None:
    credentials = tmp_path / "credentials"
    credentials.write_text(
        "TELEGRAM_API_ID=12345678\nTELEGRAM_API_HASH=0123456789abcdef0123456789abcdef\n",
        encoding="ascii",
    )
    os.chmod(credentials, 0o600)
    state_dir = tmp_path / "assistant-state"
    session = state_dir / "account"
    socket_path = tmp_path / "assistant-run" / "bridge.sock"

    config = TelegramConfig.load(
        credentials,
        session_path=session,
        state_dir=state_dir,
        socket_path=socket_path,
    )

    assert config.session_path == session
    assert config.state_dir == state_dir
    assert config.socket_path == socket_path


def test_role_bindings_round_trip_only_in_private_runtime_file(tmp_path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir(mode=0o700)
    bindings_path = state_dir / "director_roles.json"

    telegram_bridge._save_role_bindings(
        bindings_path,
        {
            "director_admin": 101,
            "director_reception": 202,
            "director_workshop": 303,
        },
    )

    assert bindings_path.stat().st_mode & 0o777 == 0o600
    assert telegram_bridge._load_role_bindings(bindings_path) == {
        "director_admin": 101,
        "director_reception": 202,
        "director_workshop": 303,
    }


def test_role_bindings_fail_closed_for_open_file_symlink_and_duplicate_peer(tmp_path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir(mode=0o700)
    bindings_path = state_dir / "director_roles.json"
    bindings_path.write_text('{"roles":{"director_admin":101},"version":1}', encoding="utf-8")
    bindings_path.chmod(0o640)
    with pytest.raises(BridgeError, match="role_bindings_invalid"):
        telegram_bridge._load_role_bindings(bindings_path)

    bindings_path.chmod(0o600)
    symlink_path = state_dir / "roles-link.json"
    symlink_path.symlink_to(bindings_path.name)
    with pytest.raises(BridgeError, match="role_bindings_unavailable"):
        telegram_bridge._load_role_bindings(symlink_path)

    with pytest.raises(BridgeError, match="role_bindings_invalid"):
        telegram_bridge._save_role_bindings(
            bindings_path,
            {"director_admin": 101, "director_reception": 101},
        )


def test_runtime_paths_reject_relative_or_cross_account_session(tmp_path) -> None:
    credentials = tmp_path / "credentials"
    credentials.write_text(
        "TELEGRAM_API_ID=12345678\nTELEGRAM_API_HASH=0123456789abcdef0123456789abcdef\n",
        encoding="ascii",
    )
    os.chmod(credentials, 0o600)

    with pytest.raises(BridgeError, match="runtime_path_not_absolute"):
        TelegramConfig.load(credentials, session_path=Path("relative/account"))
    with pytest.raises(BridgeError, match="session_state_mismatch"):
        TelegramConfig.load(
            credentials,
            session_path=tmp_path / "account-a" / "account",
            state_dir=tmp_path / "account-b",
        )


def test_parser_accepts_isolated_second_account_paths(tmp_path) -> None:
    state_dir = tmp_path / "assistant-state"
    args = build_parser().parse_args(
        [
            "--credentials",
            str(tmp_path / "credentials"),
            "--session",
            str(state_dir / "account"),
            "--state-dir",
            str(state_dir),
            "--socket",
            str(tmp_path / "assistant-run" / "bridge.sock"),
            "daemon",
        ]
    )

    assert args.command == "daemon"
    assert args.session == state_dir / "account"
    assert args.state_dir == state_dir


def test_send_contract_binds_target_text_and_expiry() -> None:
    secret = b"x" * 32
    token = issue_send_contract(secret, peer_id=10, text="hello", last_message_id=7, now=100)

    payload = verify_send_contract(token, secret, peer_id=10, text="hello", now=101)

    assert payload["last_message_id"] == 7
    with pytest.raises(BridgeError, match="contract_target_changed"):
        verify_send_contract(token, secret, peer_id=11, text="hello", now=101)
    with pytest.raises(BridgeError, match="contract_text_changed"):
        verify_send_contract(token, secret, peer_id=10, text="changed", now=101)
    with pytest.raises(BridgeError, match="contract_expired"):
        verify_send_contract(token, secret, peer_id=10, text="hello", now=100 + 901)


def test_role_send_contract_cannot_be_reused_as_direct_or_for_another_role() -> None:
    secret = b"x" * 32
    token = issue_send_contract(
        secret,
        peer_id=10,
        text="hello",
        last_message_id=7,
        role="director_admin",
        now=100,
    )

    verify_send_contract(
        token,
        secret,
        peer_id=10,
        text="hello",
        role="director_admin",
        now=101,
    )
    with pytest.raises(BridgeError, match="contract_role_required"):
        verify_send_contract(token, secret, peer_id=10, text="hello", now=101)
    with pytest.raises(BridgeError, match="contract_role_changed"):
        verify_send_contract(
            token,
            secret,
            peer_id=10,
            text="hello",
            role="director_reception",
            now=101,
        )


def test_photo_contract_binds_target_caption_photo_and_expiry() -> None:
    secret = b"x" * 32
    digest = "a" * 64
    token = issue_photo_contract(
        secret,
        peer_id=10,
        caption="hello",
        photo_sha256=digest,
        last_message_id=7,
        now=100,
    )

    payload = verify_photo_contract(
        token,
        secret,
        peer_id=10,
        caption="hello",
        photo_sha256=digest,
        now=101,
    )

    assert payload["last_message_id"] == 7
    with pytest.raises(BridgeError, match="contract_photo_changed"):
        verify_photo_contract(
            token,
            secret,
            peer_id=10,
            caption="hello",
            photo_sha256="b" * 64,
            now=101,
        )


def test_download_contract_binds_target_message_media_and_expiry() -> None:
    secret = b"x" * 32
    token = issue_download_contract(
        secret,
        peer_id=10,
        message_id=22,
        media_fingerprint="a" * 64,
        now=100,
    )

    payload = verify_download_contract(
        token,
        secret,
        peer_id=10,
        message_id=22,
        media_fingerprint="a" * 64,
        now=101,
    )

    assert payload["message_id"] == 22
    with pytest.raises(BridgeError, match="contract_message_changed"):
        verify_download_contract(
            token,
            secret,
            peer_id=10,
            message_id=23,
            media_fingerprint="a" * 64,
            now=101,
        )
    with pytest.raises(BridgeError, match="contract_media_changed"):
        verify_download_contract(
            token,
            secret,
            peer_id=10,
            message_id=22,
            media_fingerprint="b" * 64,
            now=101,
        )


def test_apply_requires_idempotency_key() -> None:
    with pytest.raises(BridgeError, match="idempotency_key_required"):
        validate_send_request({"peer": "@target", "text": "hello", "mode": "apply"})

    with pytest.raises(BridgeError, match="idempotency_key_required"):
        validate_photo_request({"peer": "@target", "photo": "/run/example.jpg", "caption": "hello", "mode": "apply"})
    with pytest.raises(BridgeError, match="idempotency_key_required"):
        validate_download_request({"peer": "@target", "message_id": 10, "mode": "apply"})


def test_only_external_apply_operations_take_the_mutation_lock() -> None:
    assert _requires_mutation_lock({"operation": "send", "mode": "apply"}) is True
    assert _requires_mutation_lock({"operation": "send_photo", "mode": "apply"}) is True
    assert _requires_mutation_lock({"operation": "send", "mode": "dry_run"}) is False
    assert _requires_mutation_lock({"operation": "download", "mode": "apply"}) is True
    assert _requires_mutation_lock({"operation": "discard_download"}) is True
    assert _requires_mutation_lock({"operation": "read"}) is False


def test_photo_file_must_be_private_jpeg_inside_outbox(tmp_path) -> None:
    outbox = tmp_path / "outbox"
    outbox.mkdir()
    outbox.chmod(0o700)
    photo = outbox / "frame.jpg"
    photo.write_bytes(b"\xff\xd8example\xff\xd9")
    os.chmod(photo, 0o600)

    resolved, digest, size = validate_photo_file(photo, outbox_dir=outbox)

    assert resolved == photo
    assert len(digest) == 64
    assert size == len(b"\xff\xd8example\xff\xd9")


def test_photo_file_rejects_open_permissions_and_wrong_directory(tmp_path) -> None:
    outbox = tmp_path / "outbox"
    outbox.mkdir()
    outbox.chmod(0o700)
    open_photo = outbox / "open.jpg"
    open_photo.write_bytes(b"\xff\xd8example\xff\xd9")
    os.chmod(open_photo, 0o640)
    outside_photo = tmp_path / "outside.jpg"
    outside_photo.write_bytes(b"\xff\xd8example\xff\xd9")
    os.chmod(outside_photo, 0o600)

    with pytest.raises(BridgeError, match="photo_permissions_invalid"):
        validate_photo_file(open_photo, outbox_dir=outbox)
    with pytest.raises(BridgeError, match="photo_path_invalid"):
        validate_photo_file(outside_photo, outbox_dir=outbox)


def test_photo_file_rejects_symlink_without_reading_target(tmp_path) -> None:
    outbox = tmp_path / "outbox"
    outbox.mkdir(mode=0o700)
    target = outbox / "target.jpg"
    target.write_bytes(b"\xff\xd8example\xff\xd9")
    target.chmod(0o600)
    photo = outbox / "frame.jpg"
    photo.symlink_to(target.name)

    with pytest.raises(BridgeError, match="photo_unavailable"):
        validate_photo_file(photo, outbox_dir=outbox)

    assert target.read_bytes() == b"\xff\xd8example\xff\xd9"


def test_send_photo_uploads_the_bytes_that_were_validated(monkeypatch, tmp_path) -> None:
    runtime_dir = tmp_path / "run"
    outbox = runtime_dir / "outbox"
    outbox.mkdir(parents=True, mode=0o700)
    photo = outbox / "frame.jpg"
    original = b"\xff\xd8validated-frame\xff\xd9"
    photo.write_bytes(original)
    photo.chmod(0o600)
    state_dir = tmp_path / "state"
    config = TelegramConfig(
        api_id=123456,
        api_hash="0" * 32,
        session_path=state_dir / "account",
        state_dir=state_dir,
        socket_path=runtime_dir / "bridge.sock",
    )
    entity = object()

    async def resolve_peer(_client, _peer):
        return entity, {"id": 10, "title": "Target", "username": None, "kind": "private"}

    async def last_message_id(_client, _entity):
        return 20

    monkeypatch.setattr(telegram_bridge, "_resolve_peer", resolve_peer)
    monkeypatch.setattr(telegram_bridge, "_last_message_id", last_message_id)

    class Client:
        uploaded = b""

        async def send_file(self, _entity, upload, **_kwargs):
            photo.write_bytes(b"\xff\xd8swapped-frame\xff\xd9")
            self.uploaded = upload.read()
            return SimpleNamespace(id=30)

        async def get_messages(self, _entity, *, ids):
            assert ids == 30
            return SimpleNamespace(message="caption", media=object())

    client = Client()
    dry_run = asyncio.run(
        telegram_bridge._handle_send_photo(
            client,
            config,
            {"peer": "10", "photo": str(photo), "caption": "caption", "mode": "dry_run"},
        )
    )
    result = asyncio.run(
        telegram_bridge._handle_send_photo(
            client,
            config,
            {
                "peer": "10",
                "photo": str(photo),
                "caption": "caption",
                "mode": "apply",
                "contract_token": dry_run["contract_token"],
                "idempotency_key": "photo-test-key",
            },
        )
    )

    assert result["verified"] is True
    assert client.uploaded == original


def test_send_request_rejects_empty_or_oversized_text() -> None:
    with pytest.raises(BridgeError, match="message_length_invalid"):
        validate_send_request({"peer": "@target", "text": "", "mode": "dry_run"})
    with pytest.raises(BridgeError, match="message_length_invalid"):
        validate_send_request({"peer": "@target", "text": "x" * 4097, "mode": "dry_run"})


def test_sensitive_telegram_and_vpn_uris_are_redacted() -> None:
    text, redacted = redact_sensitive_message_text("profiles vpn://secret-value and tg://login?token=secret-token")

    assert text == "profiles [redacted_sensitive_uri] and [redacted_sensitive_uri]"
    assert redacted is True


def test_normal_message_text_is_unchanged() -> None:
    text, redacted = redact_sensitive_message_text("Привет! Как дела?")

    assert text == "Привет! Как дела?"
    assert redacted is False


def test_save_qr_replaces_output_without_leaving_partial_file(tmp_path, monkeypatch) -> None:
    class FakeImage:
        def save(self, path, *, format) -> None:
            assert format == "PNG"
            path.write_bytes(b"\x89PNG\r\n\x1a\nexample")

    class FakeQrCode:
        @staticmethod
        def make(url):
            assert url == "tg://login?token=example"
            return FakeImage()

    monkeypatch.setattr("autostop_manager.telegram_bridge._load_qrcode", lambda: FakeQrCode())
    output = tmp_path / "login-qr.png"

    _save_qr("tg://login?token=example", output)

    assert output.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    assert output.stat().st_mode & 0o777 == 0o600
    assert list(tmp_path.glob(".login-qr.png.*.tmp")) == []


def test_one_time_password_is_private_and_consumed(tmp_path) -> None:
    password_file = tmp_path / "password.once"
    password_file.write_text("секретный пароль\n", encoding="utf-8")
    os.chmod(password_file, 0o600)

    assert _read_one_time_password(password_file) == "секретный пароль"
    assert not password_file.exists()


def test_one_time_password_rejects_open_permissions_without_consuming(tmp_path) -> None:
    password_file = tmp_path / "password.once"
    password_file.write_text("secret", encoding="utf-8")
    os.chmod(password_file, 0o640)

    with pytest.raises(BridgeError, match="two_factor_password_permissions_invalid"):
        _read_one_time_password(password_file)

    assert password_file.exists()


def _runtime_config(tmp_path) -> TelegramConfig:
    runtime_dir = tmp_path / "run"
    state_dir = tmp_path / "state"
    return TelegramConfig(
        api_id=123456,
        api_hash="0" * 32,
        session_path=state_dir / "account",
        state_dir=state_dir,
        socket_path=runtime_dir / "bridge.sock",
        role_bindings_path=state_dir / "director_roles.json",
    )


def test_target_classification_covers_private_bot_group_and_channels() -> None:
    class Utils:
        @staticmethod
        def get_peer_id(entity):
            return entity.id

    User = type("User", (), {})
    Chat = type("Chat", (), {})
    private = User()
    private.id, private.first_name, private.last_name, private.bot = 1, "A", "B", False
    bot = User()
    bot.id, bot.username, bot.bot = 2, "helper", True
    group = Chat()
    group.id, group.title = -3, "Group"
    channel = SimpleNamespace(id=-4, title="Channel", broadcast=True)
    supergroup = SimpleNamespace(id=-5, title="Supergroup", megagroup=True)

    assert telegram_bridge._target_from_entity(private, Utils)["kind"] == "private"
    assert telegram_bridge._target_from_entity(bot, Utils)["kind"] == "bot"
    assert telegram_bridge._target_from_entity(group, Utils)["kind"] == "group"
    assert telegram_bridge._target_from_entity(channel, Utils)["kind"] == "channel"
    assert telegram_bridge._target_from_entity(supergroup, Utils)["kind"] == "supergroup"


def test_read_only_bridge_operations_are_bounded_and_redacted(monkeypatch, tmp_path) -> None:
    User = type("User", (), {})
    me = User()
    me.id, me.first_name, me.last_name, me.username, me.bot = 10, "Owner", "Account", "owner", False
    entity = User()
    entity.id, entity.first_name, entity.last_name, entity.username, entity.bot = 20, "Exact", "Target", None, False

    class Utils:
        @staticmethod
        def get_peer_id(value):
            return value.id

    class Client:
        async def get_me(self):
            return me

        async def iter_dialogs(self, *, limit):
            assert limit == 2
            yield SimpleNamespace(entity=entity, unread_count=3)

        async def get_messages(self, _entity, *, limit):
            assert limit == 2
            return [
                SimpleNamespace(
                    id=2,
                    date=datetime(2026, 8, 16, tzinfo=UTC),
                    out=True,
                    message="vpn://secret",
                    media=SimpleNamespace(),
                ),
                SimpleNamespace(id=1, date=None, out=False, message="hello", media=None),
            ]

    async def resolve(_client, _peer):
        return entity, telegram_bridge._target_from_entity(entity, Utils)

    async def search(_client, query, limit):
        assert query == "Exact"
        assert limit == 3
        return [(entity, telegram_bridge._target_from_entity(entity, Utils))]

    monkeypatch.setattr(telegram_bridge, "_load_telethon", lambda: (object(), Utils, object()))
    monkeypatch.setattr(telegram_bridge, "_resolve_peer", resolve)
    monkeypatch.setattr(telegram_bridge, "_search_entities", search)
    config = _runtime_config(tmp_path)
    client = Client()

    status = asyncio.run(telegram_bridge._handle_operation(client, config, {"operation": "status"}))
    dialogs = asyncio.run(telegram_bridge._handle_operation(client, config, {"operation": "dialogs", "limit": 2}))
    search_result = asyncio.run(
        telegram_bridge._handle_operation(client, config, {"operation": "search", "query": "Exact", "limit": 3})
    )
    read = asyncio.run(
        telegram_bridge._handle_operation(client, config, {"operation": "read", "peer": "20", "limit": 2})
    )

    assert status["authorized"] is True
    assert dialogs["dialogs"][0]["unread_count"] == 3
    assert search_result["matches"][0]["id"] == 20
    assert read["messages"][1]["text"] == "[redacted_sensitive_uri]"
    assert read["messages"][1]["sensitive_content_redacted"] is True


def test_role_binding_and_role_send_are_exact_and_do_not_disclose_identity(monkeypatch, tmp_path) -> None:
    config = _runtime_config(tmp_path)
    config.state_dir.mkdir(mode=0o700)
    entity = object()
    exact_target = {
        "id": 404,
        "title": "Private identity",
        "username": "private_identity",
        "kind": "private",
        "is_contact": True,
    }

    async def resolve(_client, peer):
        assert peer in {"confirmed-contact", "404"}
        return entity, exact_target

    async def last_message_id(_client, resolved_entity):
        assert resolved_entity is entity
        return 9

    monkeypatch.setattr(telegram_bridge, "_resolve_peer", resolve)
    monkeypatch.setattr(telegram_bridge, "_last_message_id", last_message_id)

    dry_binding = asyncio.run(
        telegram_bridge._handle_operation(
            object(),
            config,
            {
                "operation": "bind_role",
                "role": "director_admin",
                "peer": "confirmed-contact",
                "mode": "dry_run",
            },
        )
    )
    applied_binding = asyncio.run(
        telegram_bridge._handle_operation(
            object(),
            config,
            {
                "operation": "bind_role",
                "role": "director_admin",
                "peer": "confirmed-contact",
                "mode": "apply",
                "contract_token": dry_binding["contract_token"],
                "idempotency_key": "bind-admin-once",
            },
        )
    )

    assert applied_binding == {
        "ok": True,
        "mode": "apply",
        "replayed": False,
        "role": "director_admin",
        "verified": True,
    }
    assert dry_binding["target"] == {
        "bound": True,
        "is_contact": True,
        "kind": "private",
        "role": "director_admin",
    }
    assert "404" not in json.dumps(dry_binding)
    assert "Private identity" not in json.dumps(dry_binding)
    assert "private_identity" not in json.dumps(dry_binding)

    roles = asyncio.run(telegram_bridge._handle_operation(object(), config, {"operation": "roles"}))
    admin = next(row for row in roles["roles"] if row["role"] == "director_admin")
    assert admin == {
        "role": "director_admin",
        "bound": True,
        "verified": True,
        "kind": "private",
    }
    assert "404" not in json.dumps(roles)

    dry_send = asyncio.run(
        telegram_bridge._handle_operation(
            object(),
            config,
            {
                "operation": "send_role",
                "role": "director_admin",
                "text": "bounded message",
                "mode": "dry_run",
            },
        )
    )
    assert dry_send["target"] == dry_binding["target"]
    assert "404" not in json.dumps(dry_send)


def test_role_binding_rejects_non_contact_and_cross_role_duplicate(monkeypatch, tmp_path) -> None:
    config = _runtime_config(tmp_path)
    config.state_dir.mkdir(mode=0o700)
    telegram_bridge._save_role_bindings(config.role_bindings_path, {"director_admin": 505})

    async def resolve(_client, peer):
        peer_id = int(peer) if peer.isdigit() else 505
        return object(), {
            "id": peer_id,
            "title": "Candidate",
            "username": None,
            "kind": "private",
            "is_contact": peer != "not-contact",
        }

    monkeypatch.setattr(telegram_bridge, "_resolve_peer", resolve)

    with pytest.raises(BridgeError, match="role_candidate_invalid"):
        asyncio.run(
            telegram_bridge._handle_operation(
                object(),
                config,
                {
                    "operation": "bind_role",
                    "role": "director_reception",
                    "peer": "not-contact",
                    "mode": "dry_run",
                },
            )
        )
    with pytest.raises(BridgeError, match="peer_already_bound"):
        asyncio.run(
            telegram_bridge._handle_operation(
                object(),
                config,
                {
                    "operation": "bind_role",
                    "role": "director_reception",
                    "peer": "confirmed-contact",
                    "mode": "dry_run",
                },
            )
        )


def test_download_media_requires_dry_run_and_saves_private_verified_file(monkeypatch, tmp_path) -> None:
    config = _runtime_config(tmp_path)
    entity = object()
    content = b"%PDF-1.7\nexample"
    message = SimpleNamespace(
        id=44,
        media=SimpleNamespace(),
        file=SimpleNamespace(name="inspection.pdf", mime_type="application/pdf", size=len(content)),
    )

    async def resolve(_client, _peer):
        return entity, {"id": 10, "title": "Target", "username": None, "kind": "private"}

    class Client:
        async def get_messages(self, _entity, *, ids):
            assert ids == 44
            return message

        async def download_media(self, exact_message, *, file):
            assert exact_message is message
            assert file is bytes
            return content

    monkeypatch.setattr(telegram_bridge, "_resolve_peer", resolve)
    client = Client()
    dry = asyncio.run(
        telegram_bridge._handle_operation(
            client,
            config,
            {"operation": "download", "peer": "10", "message_id": 44, "mode": "dry_run"},
        )
    )

    assert dry["media"]["downloadable"] is True
    applied = asyncio.run(
        telegram_bridge._handle_operation(
            client,
            config,
            {
                "operation": "download",
                "peer": "10",
                "message_id": 44,
                "mode": "apply",
                "contract_token": dry["contract_token"],
                "idempotency_key": "download-test-key",
            },
        )
    )

    saved_path = Path(applied["saved_path"])
    assert saved_path.read_bytes() == content
    assert saved_path.stat().st_mode & 0o777 == 0o600
    discarded = asyncio.run(
        telegram_bridge._handle_operation(
            client,
            config,
            {"operation": "discard_download", "path": str(saved_path)},
        )
    )
    assert discarded["removed"] is True
    assert not saved_path.exists()


def test_telegram_voice_is_downloadable_when_duration_is_bounded(monkeypatch, tmp_path) -> None:
    config = _runtime_config(tmp_path)
    entity = object()
    content = b"OggS" + b"voice-data"
    AudioAttribute = type("DocumentAttributeAudio", (), {})
    audio_attribute = AudioAttribute()
    audio_attribute.duration = 42
    audio_attribute.voice = True
    message = SimpleNamespace(
        id=46,
        media=SimpleNamespace(),
        file=SimpleNamespace(name="audio.ogg", mime_type="audio/ogg", size=len(content)),
        document=SimpleNamespace(attributes=[audio_attribute]),
    )

    async def resolve(_client, _peer):
        return entity, {"id": 10, "title": "Target", "username": None, "kind": "private"}

    class Client:
        async def get_messages(self, _entity, *, ids):
            assert ids == 46
            return message

        async def download_media(self, exact_message, *, file):
            assert exact_message is message
            assert file is bytes
            return content

    monkeypatch.setattr(telegram_bridge, "_resolve_peer", resolve)
    client = Client()
    dry = asyncio.run(
        telegram_bridge._handle_operation(
            client,
            config,
            {"operation": "download", "peer": "10", "message_id": 46, "mode": "dry_run"},
        )
    )

    assert dry["media"]["downloadable"] is True
    assert dry["media"]["voice"] is True
    assert dry["media"]["duration_seconds"] == 42
    applied = asyncio.run(
        telegram_bridge._handle_operation(
            client,
            config,
            {
                "operation": "download",
                "peer": "10",
                "message_id": 46,
                "mode": "apply",
                "contract_token": dry["contract_token"],
                "idempotency_key": "voice-download-test-key",
            },
        )
    )
    assert Path(applied["saved_path"]).suffix == ".ogg"


def test_telegram_voice_over_ten_minutes_is_not_downloadable(monkeypatch, tmp_path) -> None:
    config = _runtime_config(tmp_path)
    entity = object()
    AudioAttribute = type("DocumentAttributeAudio", (), {})
    audio_attribute = AudioAttribute()
    audio_attribute.duration = 601
    audio_attribute.voice = True
    message = SimpleNamespace(
        id=47,
        media=SimpleNamespace(),
        file=SimpleNamespace(name="audio.ogg", mime_type="audio/ogg", size=100),
        document=SimpleNamespace(attributes=[audio_attribute]),
    )

    async def resolve(_client, _peer):
        return entity, {"id": 10, "title": "Target", "username": None, "kind": "private"}

    class Client:
        async def get_messages(self, _entity, *, ids):
            assert ids == 47
            return message

    monkeypatch.setattr(telegram_bridge, "_resolve_peer", resolve)
    with pytest.raises(BridgeError, match="media_not_downloadable"):
        asyncio.run(
            telegram_bridge._handle_operation(
                Client(),
                config,
                {"operation": "download", "peer": "10", "message_id": 47, "mode": "dry_run"},
            )
        )


def test_telegram_short_mp4_video_is_downloadable(monkeypatch, tmp_path) -> None:
    config = _runtime_config(tmp_path)
    entity = object()
    content = b"\x00\x00\x00\x18ftypisom" + b"video-data"
    VideoAttribute = type("DocumentAttributeVideo", (), {})
    video_attribute = VideoAttribute()
    video_attribute.duration = 45
    video_attribute.w = 1920
    video_attribute.h = 1080
    message = SimpleNamespace(
        id=48,
        media=SimpleNamespace(),
        file=SimpleNamespace(name="clip.mp4", mime_type="video/mp4", size=len(content)),
        document=SimpleNamespace(attributes=[video_attribute]),
    )

    async def resolve(_client, _peer):
        return entity, {"id": 10, "title": "Target", "username": None, "kind": "private"}

    class Client:
        async def get_messages(self, _entity, *, ids):
            assert ids == 48
            return message

        async def download_media(self, exact_message, *, file):
            assert exact_message is message
            assert file is bytes
            return content

    monkeypatch.setattr(telegram_bridge, "_resolve_peer", resolve)
    client = Client()
    dry = asyncio.run(
        telegram_bridge._handle_operation(
            client,
            config,
            {"operation": "download", "peer": "10", "message_id": 48, "mode": "dry_run"},
        )
    )

    assert dry["media"] == {
        "downloadable": True,
        "duration_seconds": 45,
        "file_name": "clip.mp4",
        "height": 1080,
        "media_type": "SimpleNamespace",
        "mime_type": "video/mp4",
        "size_bytes": len(content),
        "suffix": ".mp4",
        "video": True,
        "voice": False,
        "width": 1920,
    }
    applied = asyncio.run(
        telegram_bridge._handle_operation(
            client,
            config,
            {
                "operation": "download",
                "peer": "10",
                "message_id": 48,
                "mode": "apply",
                "contract_token": dry["contract_token"],
                "idempotency_key": "video-download-test-key",
            },
        )
    )
    assert applied["verified"] is True
    assert Path(applied["saved_path"]).suffix == ".mp4"


@pytest.mark.parametrize("duration", [0, 121])
def test_telegram_mp4_video_requires_bounded_duration(monkeypatch, tmp_path, duration) -> None:
    config = _runtime_config(tmp_path)
    VideoAttribute = type("DocumentAttributeVideo", (), {})
    video_attribute = VideoAttribute()
    video_attribute.duration = duration
    video_attribute.w = 1280
    video_attribute.h = 720
    message = SimpleNamespace(
        id=49,
        media=SimpleNamespace(),
        file=SimpleNamespace(name="clip.mp4", mime_type="video/mp4", size=100),
        document=SimpleNamespace(attributes=[video_attribute]),
    )

    async def resolve(_client, _peer):
        return object(), {"id": 10, "title": "Target", "username": None, "kind": "private"}

    class Client:
        async def get_messages(self, _entity, *, ids):
            assert ids == 49
            return message

    monkeypatch.setattr(telegram_bridge, "_resolve_peer", resolve)
    with pytest.raises(BridgeError, match="media_not_downloadable"):
        asyncio.run(
            telegram_bridge._handle_operation(
                Client(),
                config,
                {"operation": "download", "peer": "10", "message_id": 49, "mode": "dry_run"},
            )
        )


def test_video_mp4_download_rejects_invalid_container_signature() -> None:
    with pytest.raises(BridgeError, match="download_content_invalid"):
        telegram_bridge._validate_download_content(
            b"not-an-mp4-container",
            mime_type="video/mp4",
            suffix=".mp4",
        )


def test_download_rejects_unsupported_or_oversized_media(monkeypatch, tmp_path) -> None:
    config = _runtime_config(tmp_path)
    entity = object()
    message = SimpleNamespace(
        id=45,
        media=SimpleNamespace(),
        file=SimpleNamespace(name="archive.zip", mime_type="application/zip", size=100),
    )

    async def resolve(_client, _peer):
        return entity, {"id": 10, "title": "Target", "username": None, "kind": "private"}

    class Client:
        async def get_messages(self, _entity, *, ids):
            assert ids == 45
            return message

    monkeypatch.setattr(telegram_bridge, "_resolve_peer", resolve)
    with pytest.raises(BridgeError, match="media_not_downloadable"):
        asyncio.run(
            telegram_bridge._handle_operation(
                Client(),
                config,
                {"operation": "download", "peer": "10", "message_id": 45, "mode": "dry_run"},
            )
        )


def test_discard_download_rejects_symlink(monkeypatch, tmp_path) -> None:
    config = _runtime_config(tmp_path)
    inbox = config.socket_path.parent / "inbox"
    inbox.mkdir(parents=True, mode=0o700)
    target = inbox / "target.pdf"
    target.write_bytes(b"%PDF-1.7\nexample")
    target.chmod(0o600)
    link = inbox / "link.pdf"
    link.symlink_to(target.name)

    with pytest.raises(BridgeError, match="download_unavailable"):
        asyncio.run(
            telegram_bridge._handle_operation(
                object(),
                config,
                {"operation": "discard_download", "path": str(link)},
            )
        )

    assert target.exists()


def test_text_send_dry_run_apply_replay_and_conflict(monkeypatch, tmp_path) -> None:
    config = _runtime_config(tmp_path)
    entity = object()
    target = {"id": 10, "title": "Target", "username": None, "kind": "private"}

    async def resolve(_client, _peer):
        return entity, target

    async def last_message(_client, _entity):
        return 20

    class Client:
        def __init__(self):
            self.sent: list[str] = []

        async def send_message(self, _entity, text):
            self.sent.append(text)
            return SimpleNamespace(id=30)

        async def get_messages(self, _entity, *, ids):
            assert ids == 30
            return SimpleNamespace(message=self.sent[-1])

    monkeypatch.setattr(telegram_bridge, "_resolve_peer", resolve)
    monkeypatch.setattr(telegram_bridge, "_last_message_id", last_message)
    client = Client()
    dry = asyncio.run(
        telegram_bridge._handle_operation(
            client,
            config,
            {"operation": "send", "peer": "10", "text": "hello", "mode": "dry_run"},
        )
    )
    apply_request = {
        "operation": "send",
        "peer": "10",
        "text": "hello",
        "mode": "apply",
        "contract_token": dry["contract_token"],
        "idempotency_key": "text-key",
    }
    applied = asyncio.run(telegram_bridge._handle_operation(client, config, apply_request))
    replayed = asyncio.run(telegram_bridge._handle_operation(client, config, apply_request))

    assert applied["verified"] is True
    assert replayed["replayed"] is True
    assert client.sent == ["hello"]

    changed_dry = asyncio.run(
        telegram_bridge._handle_operation(
            client,
            config,
            {"operation": "send", "peer": "10", "text": "changed", "mode": "dry_run"},
        )
    )
    with pytest.raises(BridgeError, match="idempotency_key_conflict"):
        asyncio.run(
            telegram_bridge._handle_operation(
                client,
                config,
                apply_request | {"text": "changed", "contract_token": changed_dry["contract_token"]},
            )
        )


def test_local_request_transport_and_cli_mapping(monkeypatch, tmp_path, capsys) -> None:
    response_bytes = json.dumps({"ok": True, "authorized": True}).encode() + b"\n"

    class Socket:
        def __init__(self):
            self.response = response_bytes

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def settimeout(self, timeout):
            assert timeout == 30

        def connect(self, path):
            assert path == str(tmp_path / "bridge.sock")

        def sendall(self, encoded):
            assert json.loads(encoded) == {"operation": "status"}

        def recv(self, _size):
            value, self.response = self.response, b""
            return value

    monkeypatch.setattr(telegram_bridge.socket, "socket", lambda *_args: Socket())
    result = telegram_bridge.send_local_request(tmp_path / "bridge.sock", {"operation": "status"})
    assert result["authorized"] is True

    requests = []

    def local_request(_socket_path, request):
        requests.append(request)
        return {"ok": True, "request_operation": request["operation"]}

    monkeypatch.setattr(telegram_bridge, "send_local_request", local_request)
    assert telegram_bridge.main(["--socket", str(tmp_path / "bridge.sock"), "status"]) == 0
    assert json.loads(capsys.readouterr().out)["request_operation"] == "status"
    assert (
        telegram_bridge.main(
            [
                "--socket",
                str(tmp_path / "bridge.sock"),
                "send-photo",
                "--peer",
                "10",
                "--file",
                str(tmp_path / "frame.jpg"),
                "--caption",
                "caption",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["request_operation"] == "send_photo"
    assert requests[-1]["caption"] == "caption"


def test_qr_login_handles_existing_and_new_authorization(monkeypatch, tmp_path) -> None:
    config = _runtime_config(tmp_path)
    saved_urls: list[str] = []

    class QrLogin:
        url = "tg://login?token=safe-test"

        async def wait(self):
            return SimpleNamespace(id=77)

        async def recreate(self):
            raise AssertionError("recreate is not expected")

    class Client:
        authorized = True

        def __init__(self, _session, _api_id, _api_hash):
            self.disconnected = False

        async def connect(self):
            return None

        async def disconnect(self):
            self.disconnected = True

        async def is_user_authorized(self):
            return self.authorized

        async def qr_login(self):
            return QrLogin()

    monkeypatch.setattr(telegram_bridge, "_load_telethon", lambda: (Client, object(), RuntimeError))
    monkeypatch.setattr(telegram_bridge, "_save_qr", lambda url, _path: saved_urls.append(url))

    existing = asyncio.run(telegram_bridge.run_qr_login(config, tmp_path / "existing.png"))
    assert existing == {"ok": True, "authorized": True, "already_authorized": True}

    Client.authorized = False
    created = asyncio.run(telegram_bridge.run_qr_login(config, tmp_path / "new.png"))
    assert created["account_id"] == 77
    assert created["already_authorized"] is False
    assert saved_urls == ["tg://login?token=safe-test"]


def test_daemon_creates_private_outbox_and_cleans_up(monkeypatch, tmp_path) -> None:
    config = _runtime_config(tmp_path)
    state: dict[str, bool] = {}

    class Client:
        def __init__(self, session, api_id, api_hash):
            assert session == str(config.session_path)
            assert (api_id, api_hash) == (config.api_id, config.api_hash)

        async def connect(self):
            state["connected"] = True

        async def is_user_authorized(self):
            return True

        async def disconnect(self):
            state["disconnected"] = True

    class Server:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def serve_forever(self):
            state["served"] = True

    async def start_unix_server(callback, *, path):
        assert callable(callback)
        assert path == str(config.socket_path)
        config.socket_path.touch()
        return Server()

    monkeypatch.setattr(telegram_bridge, "_load_telethon", lambda: (Client, object(), object()))
    monkeypatch.setattr(telegram_bridge.asyncio, "start_unix_server", start_unix_server)

    asyncio.run(telegram_bridge.run_daemon(config))

    outbox = config.socket_path.parent / "outbox"
    assert outbox.stat().st_mode & 0o777 == 0o700
    assert not config.socket_path.exists()
    assert state == {"connected": True, "served": True, "disconnected": True}


def test_rpc_boundary_maps_invalid_and_supported_requests(monkeypatch, tmp_path) -> None:
    config = _runtime_config(tmp_path)

    class Reader:
        def __init__(self, payload):
            self.payload = payload

        async def readline(self):
            return self.payload

    class Writer:
        def __init__(self):
            self.payload = b""

        def write(self, payload):
            self.payload += payload

        async def drain(self):
            return None

        def close(self):
            return None

        async def wait_closed(self):
            return None

    async def operation(_client, _config, request):
        return {"ok": True, "operation": request["operation"]}

    monkeypatch.setattr(telegram_bridge, "_handle_operation", operation)
    writer = Writer()
    asyncio.run(
        telegram_bridge._serve_client(
            object(),
            config,
            Reader(b'{"operation":"status"}\n'),
            writer,
        )
    )
    assert json.loads(writer.payload)["operation"] == "status"

    invalid_writer = Writer()
    asyncio.run(telegram_bridge._serve_client(object(), config, Reader(b"not-json\n"), invalid_writer))
    assert json.loads(invalid_writer.payload) == {"ok": False, "error": "request_invalid"}
