from __future__ import annotations

import os

import pytest

from autostop_manager.telegram_bridge import (
    BridgeError,
    TelegramConfig,
    _read_one_time_password,
    _save_qr,
    issue_send_contract,
    redact_sensitive_message_text,
    validate_send_request,
    verify_send_contract,
)


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


def test_apply_requires_idempotency_key() -> None:
    with pytest.raises(BridgeError, match="idempotency_key_required"):
        validate_send_request({"peer": "@target", "text": "hello", "mode": "apply"})


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
