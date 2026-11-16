"""Personal status reflects local transport state without a Telegram request."""

import asyncio
from types import SimpleNamespace

import pytest

from autostop_manager import telegram_bridge as bridge


@pytest.mark.parametrize(
    ("user_connected", "transport_connected", "ready"),
    [(True, True, True), (False, False, False), (True, False, False), (False, True, False)],
    ids=["active", "disconnected", "reconnecting", "disconnecting"],
)
def test_personal_status_does_not_report_disconnected_transport_ready(user_connected, transport_connected, ready):
    class Client:
        _autostop_authorized = True
        _autostop_cached_me = None
        _sender = SimpleNamespace(_transport_connected=lambda: transport_connected)

        def is_connected(self):
            return user_connected

        async def get_me(self):
            raise AssertionError("local status must not call Telegram")

    config = bridge.TelegramConfig(api_id=123456, api_hash="0" * 32)
    status = asyncio.run(bridge._handle_operation(Client(), config, {"operation": "status"}))

    assert status["transport_ready"] is ready
    assert status["authorized"] is True  # cached authorization is independent
    assert status["inbound_enabled"] is False


@pytest.mark.parametrize("connected", [True, False])
def test_personal_status_uses_public_connectivity_if_sender_check_is_unavailable(connected):
    client = SimpleNamespace(is_connected=lambda: connected, _autostop_authorized=True)
    config = bridge.TelegramConfig(api_id=123456, api_hash="0" * 32)

    status = asyncio.run(bridge._handle_operation(client, config, {"operation": "status"}))

    assert status["transport_ready"] is connected
    assert status["authorized"] is True


def test_legacy_client_without_connectivity_metadata_keeps_status_contract():
    client = SimpleNamespace(_autostop_authorized=True)
    config = bridge.TelegramConfig(api_id=123456, api_hash="0" * 32)

    status = asyncio.run(bridge._handle_operation(client, config, {"operation": "status"}))

    assert status["transport_ready"] is True
    assert status["authorized"] is True
