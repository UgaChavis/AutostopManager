#!/usr/bin/env bash
set -Eeuo pipefail
umask 027

PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
PYTHON="$PROJECT_ROOT/.venv/bin/python"
[[ -x "$PYTHON" ]] || {
  printf 'release_gate_error=venv_python_missing\n' >&2
  exit 1
}
cd -- "$PROJECT_ROOT"

gate_dir="$(mktemp -d /tmp/autostop-manager-release-gates.XXXXXX)"
cleanup() {
  if [[ "${gate_dir:-}" == /tmp/autostop-manager-release-gates.* \
    && -d "$gate_dir" && ! -L "$gate_dir" ]]; then
    find -P "$gate_dir" -mindepth 1 -delete
    rmdir -- "$gate_dir"
  fi
}
trap cleanup EXIT

mkdir -p "$gate_dir/tmp"
export AUTOSTOP_MANAGER_DB="$gate_dir/preflight.sqlite3"
export AUTOSTOP_MANAGER_ENV_FILE=/dev/null
export COVERAGE_FILE="$gate_dir/.coverage"
export MYPY_CACHE_DIR="$gate_dir/mypy-cache"
export RUFF_CACHE_DIR="$gate_dir/ruff-cache"
export TMPDIR="$gate_dir/tmp"
export PYTHONDONTWRITEBYTECODE=1

json_gate() {
  local name="$1"
  shift
  local report="$gate_dir/$name.json"
  local code=0

  printf '\n== %s ==\n' "$name"
  "$@" >"$report" || code=$?
  cat -- "$report"
  ((code == 0)) || return "$code"

  "$PYTHON" - "$report" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
try:
    payload = json.loads(path.read_text(encoding="utf-8"))
except (OSError, UnicodeError, json.JSONDecodeError):
    print("release_gate_report_invalid=true", file=sys.stderr)
    raise SystemExit(1)

problems = []
if not isinstance(payload, dict) or payload.get("ok") is not True:
    problems.append("ok")


def walk(value, prefix=""):
    if isinstance(value, dict):
        for key, item in value.items():
            item_path = f"{prefix}.{key}" if prefix else key
            if key in {"warnings", "missing_files"} and item:
                problems.append(item_path)
            walk(item, item_path)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            walk(item, f"{prefix}[{index}]")


walk(payload)
if problems:
    print("release_gate_report_failed=" + ",".join(sorted(set(problems))), file=sys.stderr)
    raise SystemExit(1)
PY
}

json_gate knowledge-sync "$PYTHON" -m autostop_manager.cli knowledge-sync
json_gate knowledge-audit "$PYTHON" -m autostop_manager.cli knowledge-audit
json_gate skills-audit "$PYTHON" -m autostop_manager.cli skills-audit
json_gate cleanup-audit "$PYTHON" -m autostop_manager.cli cleanup-audit

"$PYTHON" -m ruff check .
"$PYTHON" -m ruff format --check autostop_manager tests
"$PYTHON" -m mypy autostop_manager
(
  # The private parent remains 0700; a normal fixture umask keeps root runs
  # behaviorally aligned with non-root CI without exposing test artifacts.
  umask 022
  "$PYTHON" -m coverage run -m pytest -q -p no:cacheprovider \
    --basetemp "$gate_dir/pytest"
)
"$PYTHON" -m coverage report --fail-under=82

git diff --check
git diff --cached --check

cleanup
trap - EXIT
printf '\nrelease_gates_ok=true\n'
