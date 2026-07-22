#!/usr/bin/env bash
set -euo pipefail

project_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
runtime_dir=/opt/autostop-managed-hooks
requirements_dir=/etc/codex

install -d -o root -g root -m 0755 "$runtime_dir" "$requirements_dir"
install -o root -g root -m 0755 "$project_root/scripts/agent_learning_hook.py" "$runtime_dir/agent_learning_hook.py"
install -o root -g root -m 0755 "$project_root/deploy/codex/run_learning_hook.sh" "$runtime_dir/run_learning_hook.sh"
install -o root -g root -m 0644 "$project_root/deploy/codex/requirements.toml" "$requirements_dir/requirements.toml"
