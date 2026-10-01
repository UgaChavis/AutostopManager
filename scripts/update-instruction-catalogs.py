"""Index canonical project documents and selected skill entrypoints only."""

from __future__ import annotations

import argparse
from datetime import date, datetime, UTC
import json
from pathlib import Path
import re
import tomllib
from urllib.parse import unquote, urlsplit

PROJECT = Path(__file__).resolve().parents[1]
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
        inline = re.findall(r"\]\(\s*(?:<([^>\n]+)>|([^\s)]+))(?:\s+[^)]+)?\s*\)", text)
        references = re.findall(r"(?m)^ {0,3}\[[^\]\n]+\]:[ \t]*(?:<([^>\n]+)>|(\S+))", text)
        for angled, plain in (*inline, *references):
            parsed = urlsplit(angled or plain)
            if parsed.scheme or not parsed.path:
                continue
            target = path.parent / re.sub(r":\d+$", "", unquote(parsed.path))
            target = target.resolve()
            if target.is_relative_to(project) and target.suffix in {".md", ".json"}:
                pending.append(target)
    return sorted(found)


def selected_skills(project, codex):
    config_file = codex / "config.toml"
    config = tomllib.loads(config_file.read_text()) if config_file.is_file() else {}
    disabled_selectors = [s for s in config.get("skills", {}).get("config", []) if not s.get("enabled", True)]
    disabled = {Path(s["path"]).resolve() for s in disabled_selectors if s.get("path")}
    disabled_names = {s["name"] for s in disabled_selectors if s.get("name")}
    roots = [("AutoStop Manager", project / ".agents/skills"), ("Codex", codex / "skills/.system")]
    for package in PACKAGES:
        marketplace = "openai-bundled" if package == "visualize" else "openai-curated"
        if not config.get("plugins", {}).get(package + "@" + marketplace, {}).get("enabled", True):
            continue
        for distribution in ("openai-bundled", "openai-curated-remote", "openai-curated"):
            cache = codex / "plugins/cache" / distribution / package
            candidates = sorted(v / "skills" for v in cache.glob("*") if list((v / "skills").glob("*/SKILL.md")))
            if len(candidates) > 1:
                raise ValueError("Specify current package version: " + str(cache))
            if candidates:
                roots.append((package, candidates[0]))
                break
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
        f"Срез: {day}. Всего {len(skills)} навыков: проектные входы, системные средства Codex и выбранные внешние пакеты.",
        "",
        "Внешние пакеты: Gmail, Windsor.ai, GitHub, Build Web Apps, Codex Security, OpenAI Developers, Plugin Management и Visualize. Кеш остальных пакетов, вложенные справочники и шаблоны не индексируются. Наличие инструкции не подтверждает доступность коннектора.",
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
        "Область проекта: AGENTS.md, актуальные модули, проектные SKILL.md и явно связанные материалы. Исторические инструкции, отчёты, архивы и весь кеш плагинов не включаются рекурсивно. Журналы M2 находятся вне Git; вход — через [M2](M2.md).",
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
