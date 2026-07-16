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
    monkeypatch.setenv("AUTOSTOP_STORE_API_URL", "https://autostop24.shop/")
    monkeypatch.setenv("AUTOSTOP_STORE_READ_TOKEN", "runtime-only-token")

    assert config.get_db_path() == db_path.resolve()
    assert config.get_mcp_host() == "0.0.0.0"
    assert config.get_mcp_port() == 42000
    assert config.get_mcp_path() == "/manager-mcp"
    assert config.get_store_api_url() == "https://autostop24.shop/internal/agent/v1"
    assert config.get_store_read_token() == "runtime-only-token"


def test_store_api_url_rejects_external_http_and_unapproved_hosts(monkeypatch):
    monkeypatch.setenv("AUTOSTOP_STORE_API_URL", "http://autostop24.shop")
    assert config.get_store_api_url() == ""

    monkeypatch.setenv("AUTOSTOP_STORE_API_URL", "https://attacker.example")
    assert config.get_store_api_url() == ""


def test_runtime_getters_use_project_defaults(monkeypatch):
    for name in (
        "AUTOSTOP_MANAGER_DB",
        "AUTOSTOP_MANAGER_MCP_HOST",
        "AUTOSTOP_MANAGER_MCP_PORT",
        "AUTOSTOP_MANAGER_MCP_PATH",
        "AUTOSTOP_STORE_API_URL",
        "AUTOSTOP_STORE_READ_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)

    assert config.get_db_path() == config.DEFAULT_DB_PATH
    assert config.get_mcp_host() == "127.0.0.1"
    assert config.get_mcp_port() == 41931
    assert config.get_mcp_path() == "/mcp"
    assert config.get_store_api_url() == ""
    assert config.get_store_read_token() == ""
