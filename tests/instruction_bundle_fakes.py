"""Small sealed instruction releases for delivery and corruption regressions."""

from __future__ import annotations

import hashlib
from pathlib import Path

from autostop_manager.instruction_bundle import PROJECT_SKILLS


def seal_instruction_bundle(root: Path) -> None:
    """Rebuild only this synthetic release's manifest after intentional edits."""
    rows = []
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.name != "MANIFEST.sha256":
            rows.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.relative_to(root).as_posix()}\n")
    (root / "MANIFEST.sha256").write_text("".join(rows), encoding="utf-8")


def create_instruction_bundle(root: Path, revision: str = "a" * 40) -> Path:
    """Create linked profiles, a shared price rule and the five project skills."""
    files = {
        "REVISION": revision + "\n",
        "AGENTS.md": "# Canonical AutoStop\n\n[Navigation](docs/agent/modules/A1.md)\n",
        "docs/agent/modules/A1.md": "# Navigation\n\n[Client](B2.md)\n[Director](M1.md)\n[Engineer](M2.md)\n",
        "docs/agent/modules/B2.md": "# Client\n\n[Skill](../../../.agents/skills/manage-autostop-client/SKILL.md)\n",
        "docs/agent/modules/B4.md": "# Bound job\n\n[Client](B2.md)\n",
        "docs/agent/modules/F4.md": "# Prices\n\nThe client sees only the sale price; procurement stays internal.\n",
        "docs/agent/modules/M1.md": "# Director\n\n[Navigation](A1.md)\n",
        "docs/agent/modules/M2.md": "# Engineer\n\n[Navigation](A1.md)\n",
    }
    for name in PROJECT_SKILLS:
        files[f".agents/skills/{name}/SKILL.md"] = f"# {name}\n\n[Price policy](../../../docs/agent/modules/F4.md)\n"
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    seal_instruction_bundle(root)
    return root
