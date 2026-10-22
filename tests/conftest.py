"""Keep synthetic Telegram tests away from the installed account and Codex IPC."""

from pathlib import Path

import pytest

from autostop_manager import telegram_wake as wake

_INSTALLED_APP_SOCKET = Path(wake.APP_SOCKET)


def _installed_socket(path):
    target = Path(path)
    return target == _INSTALLED_APP_SOCKET or any(
        target.is_relative_to(root) for root in (Path("/run/autostop-work-telegram"), Path("/run/autostop-telegram"))
    )


@pytest.fixture(autouse=True)
def synthetic_telegram_connections(monkeypatch):
    app_connect = wake.AppServer.connect
    bridge_connect = wake.BridgeJobs._connect

    async def connect_app(app):
        if _installed_socket(app.config.app_socket):
            raise AssertionError("synthetic tests cannot connect to the installed Codex App Server")
        return await app_connect(app)

    async def connect_bridge(jobs):
        if _installed_socket(jobs.path):
            raise AssertionError("synthetic tests cannot connect to the installed Telegram bridge")
        return await bridge_connect(jobs)

    monkeypatch.setattr(wake.AppServer, "connect", connect_app)
    monkeypatch.setattr(wake.BridgeJobs, "_connect", connect_bridge)
