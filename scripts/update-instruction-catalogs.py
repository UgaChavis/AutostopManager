"""Index canonical project documents and selected skill entrypoints only."""

from __future__ import annotations

import argparse
from datetime import date, datetime, UTC
import json
from pathlib import Path
import re
import sys
import tomllib

PROJECT = Path(__file__).resolve().parents[1]
# Use this checkout's shared parsers even when invoked outside the project or
# through a venv whose editable install points at another checkout.
sys.path.insert(0, str(PROJECT))

from autostop_manager.document_links import local_document_link_target  # noqa: E402
from autostop_manager.markdown_links import visible_markdown_links  # noqa: E402

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
BLOCKED_PARTS = {"archive", "archives", "archived", "draft", "drafts"}


def cell(value):
    return re.sub(r"\s+", " ", str(value)).strip().replace("|", "\\|")


def link(label, path, project):
    target = "../../../" + str(path.relative_to(project)) if path.is_relative_to(project) else str(path)
    if " " in target:
        target = "<" + target + ">"
    return "[" + cell(label).replace("[", "(").replace("]", ")") + "](" + target + ")"


def metadata(path):
    text = path.read_text(encoding="utf-8")
    header = text.split("---", 2)[1] if text.startswith("---") else ""
    values = {}
    for key in ("name", "description"):
        match = re.search(r"^" + key + r":\s*(.*?)(?=\n[a-zA-Z][\w-]*:|\Z)", header, re.M | re.S)
        value = re.sub(r"\s+", " ", match.group(1)).strip() if match else ""
        if value[:1] in {'"', "'"} and value[-1:] == value[:1]:
            value = value[1:-1]
        values[key] = value.lstrip("> |").strip()
    if not all(values.values()):
        raise ValueError("Incomplete skill metadata: " + str(path))
    return values


def title(path):
    text = path.read_text(encoding="utf-8")
    match = re.search(r"^#\s+(.+)$", text, re.M)
    return match.group(1).strip() if match else path.stem


def project_documents(project):
    """Follow explicit project links, never generated catalogs or external caches."""
    seeds = {project / "AGENTS.md"}
    seeds.update(p for p in (project / "docs/agent/modules").glob("*.md") if re.fullmatch(r"[A-Z]\d+", p.stem))
    seeds.update((project / ".agents/skills").glob("*/SKILL.md"))
    pending = sorted(seeds)
    found = set()
    while pending:
        path = pending.pop().resolve()
        if path in found:
            continue
        if not path.is_relative_to(project) or not path.is_file():
            raise ValueError("Missing or outside-project instruction: " + str(path))
        if set(path.relative_to(project).parts) & BLOCKED_PARTS or path.name.endswith("-draft.md"):
            raise ValueError("Retired instruction is still linked: " + str(path))
        found.add(path)
        if path.suffix != ".md" or path.name in {"A4.md", "A5.md"}:
            continue
        text = path.read_text(encoding="utf-8")
        for target_link in visible_markdown_links(text):
            destination = local_document_link_target(target_link)
            if destination is None:
                continue
            target = path.parent / destination
            target = target.resolve()
            if target.is_relative_to(project) and target.suffix in {".md", ".json"}:
                pending.append(target)
    return sorted(found)


def installed_skill_root(package, codex, config):
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
    return root


def selected_skills(project, codex):
    config_file = codex / "config.toml"
    config = tomllib.loads(config_file.read_text()) if config_file.is_file() else {}
    disabled_selectors = [s for s in config.get("skills", {}).get("config", []) if not s.get("enabled", True)]
    disabled = {Path(s["path"]).resolve() for s in disabled_selectors if s.get("path")}
    disabled_names = {s["name"] for s in disabled_selectors if s.get("name")}
    roots = [("AutoStop Manager", project / ".agents/skills"), ("Codex", codex / "skills/.system")]
    for package in PACKAGES:
        root = installed_skill_root(package, codex, config)
        if root is not None:
            roots.append((package, root))
    result = []
    for group, root in roots:
        for path in sorted(root.glob("*/SKILL.md")):
            if path.resolve() in disabled or path.parent.resolve() in disabled or path.parent.name == "review-agent":
                continue
            info = metadata(path)
            name = info["name"] if group in {"AutoStop Manager", "Codex"} else group + ":" + info["name"]
            if disabled_names.intersection({name, info["name"]}):
                continue
            result.append({"group": group, "path": path, "name": name, "description": info["description"]})
    return result


def build_catalogs(project=PROJECT, codex=CODEX, day=None):
    project = project.resolve()
    day = day or datetime.now(UTC).strftime("%Y-%m-%d")
    skills = selected_skills(project, codex)
    project_paths = project_documents(project)
    external = [s for s in skills if not s["path"].is_relative_to(project)]
    documents = len(project_paths) + len(external)
    a4 = [
        "# A4 — Каталог действующих навыков",
        "",
        "Выбери навык по задаче и прочитай его актуальный SKILL.md. Проектные навыки ведут к основным инструкциям модулей; параметры инструментов проверяй в текущей схеме.",
        "",
        f"Срез: {day}. Всего {len(skills)} входов SKILL.md: проектные навыки, системные средства Codex и выбранные внешние пакеты. Это выбранные материалы; число включённых навыков реестра сеанса может отличаться.",
        "",
        "Внешние пакеты: Gmail, Windsor.ai, GitHub, Build Web Apps, Codex Security, OpenAI Developers, Plugin Management и Visualize. Входы берутся из manifest установленного пакета; пакет без навыков остаётся доступен через свои инструменты. Старые версии кеша, вложенные справочники и шаблоны не индексируются. Наличие инструкции не подтверждает доступность коннектора.",
        "",
        "Основные правила и технические справочники проекта — в [A5](A5.md); порядок работы ролей — в [M1](M1.md) и [M2](M2.md).",
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
        "Открой основной модуль или связанный технический справочник по задаче. Полный текст каждой проектной инструкции хранится в одном исходном файле. Навыки выбираются через [A4](A4.md).",
        "",
        f"Срез: {day}. Всего {documents} файлов: {len(project_paths)} в AutoStop Manager и {len(external)} входов во внешние и системные навыки Codex.",
        "",
        "Область проекта: AGENTS.md, актуальные модули, проектные SKILL.md и явно связанные материалы. Внешние входы выбираются по manifest установленного пакета. Исторические инструкции, отчёты, архивы и весь кеш плагинов не включаются рекурсивно. Журналы M2 находятся вне Git; вход — через [M2](M2.md).",
        "",
        "Обновление: `python scripts/update-instruction-catalogs.py`. Проверка без записи: `python scripts/update-instruction-catalogs.py --check`. При нескольких версиях выбранного пакета сначала установи текущую версию; доступность действий проверяй по инструментам сеанса.",
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
        document_title = generated_titles[path] if path in generated_titles else title(path)
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Compare catalogs without changing files")
    args = parser.parse_args()
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
