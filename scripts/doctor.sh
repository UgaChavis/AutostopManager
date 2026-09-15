#!/usr/bin/env bash
set -Eeuo pipefail
project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
exec "$project_root/.venv/bin/python" -m autostop_manager.cli doctor "$@"
