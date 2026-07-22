#!/usr/bin/env bash
set -euo pipefail

export AUTOSTOP_MANAGER_ROOT=/opt/AutostopManager
exec /usr/bin/python3 /opt/autostop-managed-hooks/agent_learning_hook.py
