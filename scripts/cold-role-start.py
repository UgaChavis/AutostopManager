"""Fixed cold source entrypoint; the native guard admits this exact script first."""

from __future__ import annotations

import argparse
import importlib
from pathlib import Path
import re
import sys


PROFILES = {
    "manager-mcp": ("mcp_server", ()),
    "j1": ("j1_research", ("worker",)),
    "scheduler": ("automation_daemon", ("daemon",)),
    "work-telegram": ("telegram_bridge", ("--account", "work", "daemon")),
    "personal-telegram": ("telegram_bridge", ("--account", "personal", "daemon")),
    "wake": ("telegram_wake", ("daemon",)),
}


def _source_root() -> Path:
    script = Path(__file__).absolute()
    source = script.parents[1]
    if script != script.resolve() or not re.fullmatch(
        r"/opt/autostop-manager-cold-releases/[0-9a-f]{40}/source", str(source)
    ):
        raise SystemExit("cold_bootstrap_source_invalid")
    return source


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", required=True, choices=tuple(PROFILES))
    role = parser.parse_args(argv).role
    source = _source_root()
    loaded = sys.modules.get("autostop_manager")
    if loaded is not None and Path(getattr(loaded, "__file__", "")).resolve() != (
        source / "autostop_manager/__init__.py"
    ):
        raise SystemExit("cold_bootstrap_source_already_loaded")
    # -I ignores cwd/PYTHONPATH. Only the exact guard-pinned source is added;
    # dependencies come from the separately pinned candidate venv's normal site.
    sys.path.insert(0, str(source))
    cold = importlib.import_module("autostop_manager.cold_runtime")
    current = cold.claim_runtime_start()
    if current is None:
        raise SystemExit("cold_bootstrap_binding_required")
    cold.require_role(role)
    if current.document["paths"]["source_root"] != str(source):
        raise SystemExit("cold_bootstrap_binding_source_changed")
    if current.document.get("schema") == "autostop.manager.component-runtime-binding.v1":
        # The privileged bootstrap deliberately supplies a clean environment.
        # J1 and Telegram entrypoints do not load this file themselves. Read the
        # exact protected role file after the claim, before any consumer import.
        importlib.import_module("autostop_manager.config").load_runtime_env()
    module, arguments = PROFILES[role]
    sys.argv = [str(source / "scripts/cold-role-start.py"), *arguments]
    result = importlib.import_module("autostop_manager." + module).main()
    return 0 if result is None else int(result)


if __name__ == "__main__":
    raise SystemExit(main())
