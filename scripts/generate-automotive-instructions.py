#!/usr/bin/env python3
"""Render compact operation instructions from the curated registry and MCP schemas."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def module_link(code: str) -> str:
    if re.fullmatch(r"E(?:[1-9]|1[0-5])", code):
        return f"[{code}](../modules/{code}.md)"
    return code


def render_card(tool: dict[str, Any], schemas: dict[str, Any]) -> str:
    invocation = tool["invocation"]
    lines = [
        f"# {tool['tool_id']} — {tool['title']}",
        "",
        tool["purpose"],
        "",
        f"Основной модуль: {module_link(tool['primary_module'])}; другие модули: {', '.join(module_link(code) for code in tool['also_used_in']) or 'нет'}.",
        f"Классификация: {tool['classification']}. Состояние реализации: {tool['implementation_state']}.",
        f"Источник: {tool['provider_id']}; первичная база: {tool['primary_data_source']}. Исполнение: {tool['execution_kind']}.",
        "",
    ]
    if invocation:
        lines.append(f"Вызов: native Manager MCP `{invocation['tool_name']}`.")
        for key in ("operation", "api_method", "provider"):
            if key in invocation:
                lines.append(f"{key}: `{invocation[key]}`.")
        lines.extend(
            [
                "",
                "Входы: " + ", ".join(f"`{f}`" for f in tool["input_fields"]) + ".",
                "Defaults: `" + compact(tool["defaults"]) + "`.",
            ]
        )
        schema = schemas[invocation["tool_name"]]
        required = schema.get("required", [])
        lines.append(
            "Обязательные facade поля: "
            + (", ".join(f"`{f}`" for f in required) or "нет; ограничения конкретной операции всё равно применяются")
            + "."
        )
        lines.append(
            "Fingerprint проверяет [manifest](../manager_mcp_catalog.json); полная inputSchema берётся из регистрации и `tools/list`, в CRM — native_schemas того же bundle."
        )
        lines.extend(
            [
                "",
                "Синтетический вход (форма, не утверждение о реальном автомобиле/артикуле):",
                "```json",
                compact(tool["example"]),
                "```",
                "Вход, отклоняемый схемой до исполнения инструмента:",
                "```json",
                compact(tool["invalid_example"]),
                "```",
            ]
        )
    else:
        lines.extend(
            [
                "Рабочий invocation пока отсутствует. Внешняя база/лицензия/доступ должны быть отдельно подготовлены; ссылка не является API."
            ]
        )
    lines.extend(
        ["", "Выход: " + "; ".join(tool["outputs"]) + ".", "Ошибки и неполнота: " + "; ".join(tool["errors"]) + ".", ""]
    )
    lines.extend("- " + text for text in tool["limitations"])
    reference = os.path.relpath(ROOT / tool["reference"], (ROOT / tool["instruction_ref"]).parent)
    lines.extend(
        [
            "",
            f"Подробный контракт: [справочник]({reference}). Карточка и inputSchema согласованы versioned export; ручной цвет не является результатом проверки.",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    from mcp.server.fastmcp import FastMCP
    from autostop_manager.mcp_tools import register_manager_tools

    registry = json.loads((ROOT / "docs/agent/automotive_tools.json").read_text())
    with tempfile.TemporaryDirectory(prefix="catalog-schema-") as temp:
        os.environ["AUTOSTOP_MANAGER_ENV_FILE"] = os.devnull
        os.environ["AUTOSTOP_MANAGER_DB"] = str(Path(temp) / "schema.sqlite3")
        server = FastMCP("instruction-generation")
        register_manager_tools(server)
        schemas = {name: tool.parameters for name, tool in server._tool_manager._tools.items()}
        mismatches = []
        for tool in registry["tools"]:
            text = render_card(tool, schemas)
            target = ROOT / tool["instruction_ref"]
            if args.check:
                if not target.is_file() or target.read_text() != text:
                    mismatches.append(tool["instruction_ref"])
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text, encoding="utf-8")
        print(compact({"ok": not mismatches, "cards": len(registry["tools"]), "mismatches": mismatches}))
        if mismatches:
            raise SystemExit(1)


if __name__ == "__main__":
    main()
