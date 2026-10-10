"""Index canonical project documents and selected skill entrypoints only."""

from __future__ import annotations

import argparse
from datetime import date, datetime, UTC
import json
from pathlib import Path
import re
import sys
import tomllib

import yaml

PROJECT = Path(__file__).resolve().parents[1]
# Use this checkout's shared parsers even when invoked outside the project or
# through a venv whose editable install points at another checkout.
sys.path.insert(0, str(PROJECT))

from autostop_manager.document_links import document_source_lines  # noqa: E402
from autostop_manager.instruction_inventory import (  # noqa: E402
    active_instruction_candidates,
    collect_instruction_inventory,
    read_instruction_bytes,
    require_instruction_inventory,
)
from autostop_manager.markdown_links import markdown_section_body  # noqa: E402

CODEX = Path("/root/.codex")
PACKAGES = (
    "gmail",
    "windsor-ai",
    "github",
    "build-web-apps",
    "codex-security",
    "openai-developers",
    "plugin-management",
    "visualize",
)


def cell(value):
    return re.sub(r"\s+", " ", str(value)).strip().replace("|", "\\|")


def link(label, path, project):
    target = "../../../" + str(path.relative_to(project)) if path.is_relative_to(project) else str(path)
    if " " in target:
        target = "<" + target + ">"
    return "[" + cell(label).replace("[", "(").replace("]", ")") + "](" + target + ")"


def metadata(path):
    text = read_instruction_bytes(path).decode("utf-8")
    lines = document_source_lines(text)
    end = next((index for index, line in enumerate(lines[1:], 1) if line.rstrip(" \t") == "---"), None)
    if not lines or lines[0].rstrip(" \t") != "---" or end is None:
        raise ValueError("Incomplete skill metadata: " + str(path))
    try:
        header = yaml.safe_load("\n".join(lines[1:end]))
    except yaml.YAMLError:
        raise ValueError("Invalid skill metadata: " + str(path)) from None
    if not isinstance(header, dict) or any(
        not isinstance(header.get(key), str) or not header[key].strip() for key in ("name", "description")
    ):
        raise ValueError("Incomplete skill metadata: " + str(path))
    return {key: re.sub(r"\s+", " ", header[key]).strip() for key in ("name", "description")}


def title(path, content=None):
    text = (read_instruction_bytes(path) if content is None else content).decode("utf-8")
    match = re.search(r"^#\s+(.+)$", text, re.M)
    return match.group(1).strip() if match else path.stem


def project_documents(project):
    """Follow explicit project links, never generated catalogs or external caches."""
    return require_instruction_inventory(collect_instruction_inventory(project))


def installed_skill_root(package, codex, config):
    location = _installed_skill_location(package, codex, config)
    return location[0] if location is not None else None


def _installed_skill_location(package, codex, config):
    """Resolve installation metadata before inspecting skill files in its cache."""
    caches = codex / "plugins/cache"
    remote = caches / "openai-curated-remote" / package
    marker = remote / ".codex-remote-plugin-install.json"
    plugins = config.get("plugins", {})
    if not isinstance(plugins, dict):
        raise ValueError("Invalid plugin installation selectors")
    if marker.is_file():
        installation = json.loads(marker.read_text(encoding="utf-8"))
        if (
            not isinstance(installation, dict)
            or type(installation.get("schema_version")) is not int
            or installation.get("schema_version") != 1
            or not isinstance(installation.get("remote_plugin_id"), str)
            or not installation["remote_plugin_id"].strip()
        ):
            raise ValueError("Invalid installed package marker: " + str(marker))
        marketplace = "openai-curated-remote"
        selector = plugins.get(package + "@" + marketplace, plugins.get(package + "@openai-curated", {}))
    else:
        installed = [m for m in ("openai-bundled", "openai-curated") if package + "@" + m in plugins]
        if not installed:
            return None
        if len(installed) != 1:
            raise ValueError("Specify installed package marketplace: " + package)
        marketplace = installed[0]
        selector = plugins[package + "@" + marketplace]
    if not isinstance(selector, dict) or type(selector.get("enabled", True)) is not bool:
        raise ValueError("Invalid installed package selector: " + package + "@" + marketplace)
    if not selector.get("enabled", True):
        return None

    cache = caches / marketplace / package
    manifests = sorted(cache.glob("*/.codex-plugin/plugin.json"))
    if len(manifests) != 1:
        raise ValueError("Specify current package version: " + str(cache))
    manifest = manifests[0]
    if not manifest.resolve().is_relative_to(cache.resolve()):
        raise ValueError("Outside-package manifest: " + str(manifest))
    info = json.loads(manifest.read_text(encoding="utf-8"))
    if not isinstance(info, dict) or info.get("name") != package:
        raise ValueError("Invalid installed package manifest: " + str(manifest))
    skill_path = info.get("skills")
    if skill_path is None:
        return None
    if not isinstance(skill_path, str) or not skill_path.strip():
        raise ValueError("Invalid installed package skills: " + str(manifest))
    version = manifest.parent.parent.resolve()
    root = (version / skill_path).resolve()
    if not root.is_relative_to(version) or not root.is_dir():
        raise ValueError("Missing or outside-package skills: " + str(manifest))
    return root, version


def selected_skills(project, codex):
    config_file = codex / "config.toml"
    config = tomllib.loads(config_file.read_text()) if config_file.is_file() else {}
    disabled_selectors = [s for s in config.get("skills", {}).get("config", []) if not s.get("enabled", True)]
    disabled = {Path(s["path"]).resolve() for s in disabled_selectors if s.get("path")}
    disabled_names = {s["name"] for s in disabled_selectors if s.get("name")}
    roots = [
        ("AutoStop Manager", project / ".agents/skills", (project / ".agents/skills").resolve()),
        ("Codex", codex / "skills/.system", (codex / "skills/.system").resolve()),
    ]
    for package in PACKAGES:
        location = _installed_skill_location(package, codex, config)
        if location is not None:
            roots.append((package, *location))
    result = []
    for group, root, boundary in roots:
        for path in sorted(root.glob("*/SKILL.md")):
            if path.resolve() in disabled or path.parent.resolve() in disabled or path.parent.name == "review-agent":
                continue
            # Validate each entry, not just the manifest's skills directory;
            # a symlink may otherwise read another installed version or host file.
            if not path.resolve().is_relative_to(boundary):
                raise ValueError("Outside-package skill: " + str(path))
            info = metadata(path)
            name = info["name"] if group in {"AutoStop Manager", "Codex"} else group + ":" + info["name"]
            if disabled_names.intersection({name, info["name"]}):
                continue
            result.append({"group": group, "path": path, "name": name, "description": info["description"]})
    return result


def project_skill_entries(project):
    """Canonical repository entries, independent of one host's skill selectors."""
    boundary = (project / ".agents/skills").resolve()
    result = []
    for path in sorted(boundary.glob("*/SKILL.md")):
        if not path.resolve().is_relative_to(boundary):
            raise ValueError("Outside-package skill: " + str(path))
        info = metadata(path)
        result.append({"group": "AutoStop Manager", "path": path, **info})
    return result


def build_catalogs(project=PROJECT, codex=CODEX, day=None, *, project_only=False):
    project = project.resolve()
    day = day or datetime.now(UTC).strftime("%Y-%m-%d")
    skills = project_skill_entries(project) if project_only else selected_skills(project, codex)
    inventory = collect_instruction_inventory(project)
    project_paths = require_instruction_inventory(inventory)
    external = [s for s in skills if not s["path"].is_relative_to(project)]
    documents = len(project_paths) + len(external)
    a4 = [
        "# A4 — Каталог действующих навыков",
        "",
        "Выбери навык по задаче и прочитай его актуальный SKILL.md. Клиентское поведение целиком задаёт manage-autostop-client после выбора роли через B2; технические операции, источники, ценовая политика и проверки остаются в модулях. Параметры инструментов проверяй в текущей схеме.",
        "",
        f"Срез: {day}. Всего {len(skills)} входов SKILL.md: проектные навыки, системные средства Codex и выбранные внешние пакеты. Это выбранные материалы; число включённых навыков реестра сеанса может отличаться.",
        "",
        "Внешние пакеты: Gmail, Windsor.ai, GitHub, Build Web Apps, Codex Security, OpenAI Developers, Plugin Management и Visualize. Входы берутся из manifest установленного пакета; пакет без навыков остаётся доступен через свои инструменты. Старые версии кеша, вложенные справочники и шаблоны не индексируются. Наличие инструкции не подтверждает доступность коннектора.",
        "",
        "Общие правила, профильные навыки и технические справочники проекта — в [A5](A5.md); порядок работы ролей — в [M1](M1.md) и [M2](M2.md).",
        "",
    ]
    for group in dict.fromkeys(s["group"] for s in skills):
        a4.extend(["## " + group, "", "| Навык | Назначение |", "| --- | --- |"])
        for s in (s for s in skills if s["group"] == group):
            a4.append("| " + link(s["name"], s["path"], project) + " | " + cell(s["description"]) + " |")
        a4.append("")
    a5 = [
        "# A5 — Указатель действующих инструкций",
        "",
        "Открой профильный навык, модуль или технический справочник по задаче. Полный текст каждой проектной инструкции хранится в одном исходном файле; клиентское поведение — в manage-autostop-client, выбранном через B2. Навыки выбираются через [A4](A4.md).",
        "",
        f"Срез: {day}. Всего {documents} файлов: {len(project_paths)} в AutoStop Manager и {len(external)} входов во внешние и системные навыки Codex.",
        "",
        "Область проекта: AGENTS.md, актуальные модули, проектные SKILL.md и явно связанные материалы. Внешние входы выбираются по manifest установленного пакета. Исторические инструкции, отчёты, архивы и весь кеш плагинов не включаются рекурсивно. Журналы M2 находятся вне Git; вход — через [M2](M2.md).",
        "",
        "Обновление: `python scripts/update-instruction-catalogs.py`. Проверка без записи на хосте: `python scripts/update-instruction-catalogs.py --check`; переносимая проверка проектной части: `python scripts/update-instruction-catalogs.py --check --project-only`. При нескольких версиях выбранного пакета сначала установи текущую версию; доступность действий проверяй по инструментам сеанса.",
        "",
        "## AutoStop Manager",
        "",
        "| Файл | Материал |",
        "| --- | --- |",
    ]
    generated_titles = {
        project / "docs/agent/modules/A4.md": a4[0].removeprefix("# "),
        project / "docs/agent/modules/A5.md": a5[0].removeprefix("# "),
    }
    for path in project_paths:
        document_title = generated_titles[path] if path in generated_titles else title(path, inventory.contents[path])
        a5.append("| " + link(str(path.relative_to(project)), path, project) + " | " + cell(document_title) + " |")
    a5.extend(["", "## Внешние и системные навыки Codex", "", "| Пакет | Вход |", "| --- | --- |"])
    for s in external:
        a5.append("| " + s["group"] + " | " + link(s["name"], s["path"], project) + " |")
    a5.append("")
    summary = {
        "skills": len(skills),
        "documents": documents,
        "project_documents": len(project_paths),
        "codex_documents": len(external),
        "project_paths": [str(p.relative_to(project)) for p in project_paths],
    }
    return "\n".join(a4).rstrip() + "\n", "\n".join(a5).rstrip() + "\n", summary


def checked_catalog_day(project):
    days = []
    for name in ("A4.md", "A5.md"):
        path = project / "docs/agent/modules" / name
        if not path.is_file():
            return None
        match = re.search(r"(?m)^Срез: (\d{4}-\d{2}-\d{2})\.", path.read_text(encoding="utf-8"))
        if not match:
            return None
        try:
            date.fromisoformat(match.group(1))
        except ValueError:
            return None
        days.append(match.group(1))
    return days[0] if days[0] == days[1] else None


def check_project_catalogs(project=PROJECT):
    """Check repository coverage and generated project sections without Codex IO."""
    project = project.resolve()
    day = checked_catalog_day(project)
    a4, a5, summary = build_catalogs(project, day=day, project_only=True)
    expected_paths = {project / name for name in summary["project_paths"]}
    candidates = active_instruction_candidates(project)
    orphaned = sorted(
        path.relative_to(project).as_posix() for path in candidates if path.resolve() not in expected_paths
    )
    mismatches = []
    for name, expected in (("A4.md", a4), ("A5.md", a5)):
        path = project / "docs/agent/modules" / name
        if not path.is_file():
            mismatches.append(name)
            continue
        actual = read_instruction_bytes(path).decode("utf-8")
        actual_section = markdown_section_body(actual, "AutoStop Manager")
        if actual_section != markdown_section_body(expected, "AutoStop Manager"):
            mismatches.append(name)
        if name == "A5.md":
            count = re.search(r"(?m)^Срез: \d{4}-\d{2}-\d{2}\. Всего \d+ файлов: (\d+) в AutoStop Manager\b", actual)
            if count is None or int(count[1]) != summary["project_documents"]:
                mismatches.append(name)
    if day is None:
        mismatches.extend(["A4.md", "A5.md"])
    return {
        "ok": not mismatches and not orphaned,
        "project_only": True,
        "project_documents": summary["project_documents"],
        "project_skills": summary["skills"],
        "active_document_candidates": len(candidates),
        "orphaned_active_documents": orphaned[:64],
        "orphaned_active_document_count": len(orphaned),
        "mismatches": sorted(set(mismatches)),
        "external_skills_checked": False,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Compare catalogs without changing files")
    parser.add_argument(
        "--project-only", action="store_true", help="Check repository sections without host Codex files"
    )
    args = parser.parse_args()
    if args.project_only:
        if not args.check:
            parser.error("--project-only requires --check; host catalogs must not be overwritten")
        summary = check_project_catalogs(PROJECT)
        print(json.dumps(summary, ensure_ascii=False))
        return int(not summary["ok"])
    # A read-only check compares content at the recorded cutoff; tomorrow's
    # date alone does not make an unchanged inventory stale.
    day = checked_catalog_day(PROJECT) if args.check else None
    a4, a5, summary = build_catalogs(PROJECT, CODEX, day)
    mismatches = []
    for name, text in [("A4.md", a4), ("A5.md", a5)]:
        path = PROJECT / "docs/agent/modules" / name
        if args.check:
            if not path.is_file() or path.read_text(encoding="utf-8") != text:
                mismatches.append(name)
        else:
            path.write_text(text, encoding="utf-8")
    if args.check:
        summary["ok"] = not mismatches
        summary["mismatches"] = mismatches
    print(json.dumps(summary, ensure_ascii=False))
    return int(bool(mismatches))


if __name__ == "__main__":
    raise SystemExit(main())
