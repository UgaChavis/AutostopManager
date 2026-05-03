# AutostopManager Deployment Runbook

Purpose: keep the manager memory/MCP layer reproducible without leaking CRM
data.

## What Can Be Published

Safe to commit and push:

- Python package code in `autostop_manager/`
- tests in `tests/`
- playbooks and source-routing catalogs in `docs/`
- project metadata such as `README.md`, `pyproject.toml`, `.gitignore`

Do not commit or push:

- `data/` runtime artifacts
- SQLite memory databases
- CRM board snapshots
- client names, phones, payments, VIN/license snapshots, or repair-order dumps
- temporary web/browser/test caches

## Local Verification

Run before publishing:

```powershell
python -m pytest -q
```

Optional manual checks:

```powershell
python -m autostop_manager.cli today
python -m autostop_manager.cli service-plan --area parts --city Красноярск --vehicle "Lexus RX200T" --part-number 90311-89014 --urgency today
```

## Local MCP Server

Default local command:

```powershell
python -m autostop_manager.mcp_server
```

Optional environment:

```powershell
$env:AUTOSTOP_MANAGER_MCP_HOST = "127.0.0.1"
$env:AUTOSTOP_MANAGER_MCP_PORT = "41931"
$env:AUTOSTOP_MANAGER_MCP_PATH = "/mcp"
$env:AUTOSTOP_MANAGER_DB = "C:\path\to\autostop_manager.sqlite3"
```

## Server Deployment Requirements

A real server deployment needs one explicit target:

- host/IP or platform
- SSH/user or deployment token
- service manager: systemd, Docker, PM2, Cloudflare, or another platform
- persistent path for `AUTOSTOP_MANAGER_DB`
- network exposure rule for the MCP endpoint
- restart/smoke-check command

Do not invent deployment credentials or push private runtime data to make a
deployment appear complete.

## Server Smoke Check

After deployment, verify:

- service process is running
- MCP endpoint answers on configured host/port/path
- `today_context` works
- `recommend_service_management_actions` works
- logs do not contain secrets or CRM dumps
