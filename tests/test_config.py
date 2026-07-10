from __future__ import annotations

import pytest

from autostop_manager.config import get_mcp_host


def test_mcp_host_defaults_to_loopback(monkeypatch):
    monkeypatch.delenv("AUTOSTOP_MANAGER_MCP_HOST", raising=False)

    assert get_mcp_host() == "127.0.0.1"


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost"])
def test_mcp_host_accepts_only_loopback(monkeypatch, host):
    monkeypatch.setenv("AUTOSTOP_MANAGER_MCP_HOST", host)

    assert get_mcp_host() == host


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.0.2.10", "manager.internal"])
def test_mcp_host_rejects_unauthenticated_non_loopback(monkeypatch, host):
    monkeypatch.setenv("AUTOSTOP_MANAGER_MCP_HOST", host)

    with pytest.raises(ValueError, match="loopback"):
        get_mcp_host()
