from __future__ import annotations

import os

from autostop_manager import config


def test_configured_env_file_list_trims_entries_and_ignores_empty_items(monkeypatch, tmp_path):
    first = tmp_path / "first.env"
    second = tmp_path / "second.env"
    monkeypatch.setenv(
        "AUTOSTOP_MANAGER_ENV_FILE",
        f"  {first}  {os.pathsep}{os.pathsep}  {second}  ",
    )

    assert config._iter_runtime_env_files() == [first, second]


def test_load_runtime_env_parses_safe_assignments_without_overriding_process_env(monkeypatch, tmp_path):
    env_file = tmp_path / "manager.env"
    env_file.write_text(
        "\n".join(
            [
                "# comment",
                "VALID_NAME='loaded value'",
                'DOUBLE_QUOTED="second value"',
                "EXISTING_NAME=file value",
                "9INVALID=ignored",
                "INVALID-NAME=ignored",
                "NO_ASSIGNMENT",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("AUTOSTOP_MANAGER_ENV_FILE", str(env_file))
    monkeypatch.setenv("EXISTING_NAME", "process value")
    monkeypatch.delenv("VALID_NAME", raising=False)
    monkeypatch.delenv("DOUBLE_QUOTED", raising=False)
    monkeypatch.setattr(config, "_ENV_LOADED", False)

    assert config.load_runtime_env() == [env_file]
    assert os.environ["VALID_NAME"] == "loaded value"
    assert os.environ["DOUBLE_QUOTED"] == "second value"
    assert os.environ["EXISTING_NAME"] == "process value"
    assert "9INVALID" not in os.environ
    assert "INVALID-NAME" not in os.environ
    assert config.load_runtime_env() == []


def test_load_runtime_env_skips_missing_paths_and_force_rechecks(monkeypatch, tmp_path):
    missing = tmp_path / "missing.env"
    monkeypatch.setenv("AUTOSTOP_MANAGER_ENV_FILE", str(missing))
    monkeypatch.setattr(config, "_ENV_LOADED", False)

    assert config.load_runtime_env() == []
    assert config.load_runtime_env(force=True) == []


def test_runtime_getters_normalize_configured_values(monkeypatch, tmp_path):
    db_path = tmp_path / "manager.sqlite3"
    monkeypatch.setenv("AUTOSTOP_MANAGER_DB", str(db_path))
    monkeypatch.setenv("AUTOSTOP_MANAGER_MCP_HOST", "0.0.0.0")
    monkeypatch.setenv("AUTOSTOP_MANAGER_MCP_PORT", "42000")
    monkeypatch.setenv("AUTOSTOP_MANAGER_MCP_PATH", "manager-mcp")

    assert config.get_db_path() == db_path.resolve()
    assert config.get_mcp_host() == "0.0.0.0"
    assert config.get_mcp_port() == 42000
    assert config.get_mcp_path() == "/manager-mcp"


def test_runtime_getters_use_project_defaults(monkeypatch):
    for name in (
        "AUTOSTOP_MANAGER_DB",
        "AUTOSTOP_MANAGER_MCP_HOST",
        "AUTOSTOP_MANAGER_MCP_PORT",
        "AUTOSTOP_MANAGER_MCP_PATH",
    ):
        monkeypatch.delenv(name, raising=False)

    assert config.get_db_path() == config.DEFAULT_DB_PATH
    assert config.get_mcp_host() == "127.0.0.1"
    assert config.get_mcp_port() == 41931
    assert config.get_mcp_path() == "/mcp"


def test_store_runtime_getters_use_only_explicit_injected_environment_names(monkeypatch):
    monkeypatch.setenv("AUTOSTOP_STORE_API_URL", "http://autostop-app:8000/")
    monkeypatch.setenv("AUTOSTOP_STORE_READ_TOKEN", "read-runtime-token")
    monkeypatch.setenv("AUTOSTOP_STORE_QUOTE_TOKEN", "quote-runtime-token")
    monkeypatch.setenv("AUTOSTOP_STORE_MANAGE_TOKEN", "manage-runtime-token")
    monkeypatch.setenv("STORE_ADMIN_PASSWORD", "must-not-be-used")

    assert config.get_store_api_url() == "http://autostop-app:8000/internal/agent/v1"
    assert config.get_store_read_token() == "read-runtime-token"
    assert config.get_store_quote_token() == "quote-runtime-token"
    assert config.get_store_manage_token() == "manage-runtime-token"


def test_store_api_url_does_not_duplicate_agent_prefix(monkeypatch):
    monkeypatch.setenv("AUTOSTOP_STORE_API_URL", "http://autostop-app:8000/internal/agent/v1/")

    assert config.get_store_api_url() == "http://autostop-app:8000/internal/agent/v1"


def test_store_api_url_allows_bounded_loopback_endpoints_for_local_tests(monkeypatch):
    for value, expected in (
        ("http://127.0.0.1:18000", "http://127.0.0.1:18000/internal/agent/v1"),
        ("http://localhost:18001/internal/agent/v1", "http://localhost:18001/internal/agent/v1"),
        ("http://[::1]:18002/", "http://[::1]:18002/internal/agent/v1"),
    ):
        monkeypatch.setenv("AUTOSTOP_STORE_API_URL", value)
        assert config.get_store_api_url() == expected


def test_store_api_url_rejects_external_hosts_credentials_and_url_smuggling(monkeypatch):
    rejected = (
        "http://evil.example:8000",
        "https://autostop-app:8000",
        "http://autostop-app:80",
        "http://user:password@autostop-app:8000",
        "http://autostop-app:8000@evil.example",
        "http://autostop-app:8000/internal/agent/v1?token=secret",
        "http://autostop-app:8000/internal/agent/v1#fragment",
        "http://autostop-app:8000/other-path",
        "http://127.0.0.1",
    )

    for value in rejected:
        monkeypatch.setenv("AUTOSTOP_STORE_API_URL", value)
        assert config.get_store_api_url() == ""
