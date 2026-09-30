"""Rebuild the Manager catalogs from current local instruction sources."""

from datetime import datetime, UTC
from pathlib import Path
import json
import re

PROJECT = Path(__file__).resolve().parents[1]
OUT = PROJECT / "docs/agent/modules"
DAY = datetime.now(UTC).strftime("%Y-%m-%d")
ROOTS = [("AutoStop Manager", PROJECT / ".agents/skills"), ("Codex", Path("/root/.codex/skills/.system"))]
# Current installed package trees. Remote and CLI use separate distributions.
for cache in [
    Path("/root/.codex/plugins/cache/openai-bundled"),
    Path("/root/.codex/plugins/cache/openai-curated-remote"),
]:
    for package in sorted(cache.iterdir()):
        if not package.is_dir():
            continue
        versions = [v for v in package.iterdir() if v.is_dir()]
        if len(versions) != 1:
            raise RuntimeError("Specify the current package version before rebuilding: " + str(package))
        ROOTS.append((package.name, versions[0]))
# These enabled CLI packages carry skills absent from their remote connector-only bundles.
for package in ["gmail", "google-calendar", "github"]:
    root = Path("/root/.codex/plugins/cache/openai-curated") / package
    versions = [v for v in root.iterdir() if v.is_dir()] if root.is_dir() else []
    if len(versions) == 1:
        ROOTS.append((package + " (CLI)", versions[0]))
    elif len(versions) > 1:
        raise RuntimeError("Specify the current CLI package version before rebuilding: " + str(root))

INTERNAL = {
    "Codex": {"review-agent"},
    "data-analytics": {
        "convert-to-doc",
        "convert-to-slides",
        "report-to-pdf",
        "schedule-refresh-jobs",
        "share-artifact-summary",
    },
    "product-design": {"design-qa", "get-context", "research", "share", "user-context"},
}
TEXT_EXT = {".md", ".mdx", ".rst", ".txt", ".prompt", ".jinja", ".jinja2"}
BLOCKED_PARTS = {
    ".git",
    "node_modules",
    "__pycache__",
    "archive",
    "archives",
    "archived",
    "draft",
    "drafts",
    "license",
    "licenses",
    "licences",
}
BLOCKED_NAMES = {
    "license",
    "license.md",
    "license.txt",
    "notice",
    "notice.txt",
    "changelog",
    "changelog.md",
    "third_party_notices.txt",
}


def cell(value):
    return re.sub(r"\s+", " ", str(value)).strip().replace("|", "\\|")


def link(label, path):
    target = "../../../" + str(path.relative_to(PROJECT)) if path.is_relative_to(PROJECT) else str(path)
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
    return values


def title(path):
    text = path.read_text(encoding="utf-8", errors="replace")
    match = re.search(r"^#\s+(.+)$", text, re.M)
    return match.group(1).strip() if match else path.stem


def eligible(path):
    parts = {part.lower() for part in path.parts}
    if parts & BLOCKED_PARTS or path.name.lower() in BLOCKED_NAMES:
        return False
    if path.name.lower().endswith("-draft.md"):
        return False
    if path.name.lower().startswith("requirements") and path.suffix.lower() == ".txt":
        return False
    if path.suffix.lower() in TEXT_EXT or (path.is_relative_to(PROJECT / "docs") and path.suffix == ".json"):
        return True
    return (
        path.parent.name == "agents"
        and path.suffix.lower() in {".yaml", ".yml"}
        and re.search(r"^\s*default_prompt:", path.read_text(), re.M) is not None
    )


def kind(path):
    if path.name == "SKILL.md":
        return "Навык"
    if path.name == "AGENTS.md":
        return "Общие правила"
    if path.suffix in {".yaml", ".yml"}:
        return "Начальный запрос навыка"
    if path.suffix == ".json":
        return "Реестр / контракт"
    if "template" in str(path).lower() or "example" in str(path).lower():
        return "Шаблон / пример"
    return "Инструкция / справочник"


skills = []
documents = []
for group, root in ROOTS:
    if not root.is_dir():
        raise RuntimeError("Missing current package: " + str(root))
    for path in sorted(root.rglob("SKILL.md")):
        info = metadata(path)
        if not info["name"] or not info["description"]:
            raise RuntimeError("Incomplete skill metadata: " + str(path))
        internal = path.parent.name in INTERNAL.get(group, set()) or (group == "sales" and path.parent.name != "index")
        logical = info["name"] if group in {"AutoStop Manager", "Codex"} else group + ":" + info["name"]
        skills.append(
            {
                "group": group,
                "path": path,
                "name": logical,
                "description": info["description"],
                "entry": "Вложенный"
                if internal
                else "Установленный"
                if group.endswith("(CLI)") or group == "openai-templates"
                else "Основной",
            }
        )
    for path in sorted(root.rglob("*")):
        if path.is_file() and eligible(path):
            documents.append(
                {
                    "group": group,
                    "path": path,
                    "relative": str(path.relative_to(root)),
                    "title": title(path),
                    "kind": kind(path),
                }
            )


for name in ["A4.md", "A5.md"]:
    placeholder = OUT / name
    if not placeholder.exists():
        placeholder.parent.mkdir(parents=True, exist_ok=True)
        placeholder.write_text("# " + name[:-3] + "\n")
project_docs = [PROJECT / "AGENTS.md", *sorted((PROJECT / "docs").rglob("*"))]
for path in project_docs:
    if path.is_file() and eligible(path):
        documents.append(
            {
                "group": "AutoStop Manager",
                "path": path,
                "relative": str(path.relative_to(PROJECT)),
                "title": title(path),
                "kind": kind(path),
            }
        )
documents.sort(key=lambda d: (next(i for i, (group, _) in enumerate(ROOTS) if group == d["group"]), str(d["path"])))
if len({str(d["path"]) for d in documents}) != len(documents):
    raise RuntimeError("Duplicate instruction source")

main_count = sum(s["entry"] == "Основной" for s in skills)
project_count = sum(d["group"] == "AutoStop Manager" for d in documents)
plugin_count = len(documents) - project_count
a4 = [
    "# A4 — Каталог навыков",
    "",
    "A4 помогает выбрать подходящий навык. Найди его по названию или назначению, открой актуальный SKILL.md и используй по текущей задаче. Параметры доступных инструментов проверяй в живой схеме.",
    "",
    "Здесь перечислены навыки AutoStop Manager, встроенные навыки Codex и навыки текущих пакетов установленных плагинов. Дополнительные инструкции и справочники ищи в [A5 — Полный указатель](A5.md).",
    "",
    "Срез: " + DAY + ". Всего " + str(len(skills)) + " навыков в проекте и текущих установленных пакетах.",
    "",
    "«Основной» — навык основного пакета; «Вложенный» — профильная инструкция пакета; «Установленный» — дополнительный шаблон или навык отдельного CLI-пакета. Наличие файла не гарантирует доступность инструмента в текущем сеансе.",
    "",
    "Черновики инструкций, архивы и другие версии пакетов в перечень не включены. При изменении состава плагинов или версии пакета обнови ссылки и список по текущему окружению.",
    "",
]
for group, root in ROOTS:
    entries = [s for s in skills if s["group"] == group]
    if not entries:
        continue
    a4 += ["## " + group, ""]
    if group not in {"AutoStop Manager", "Codex"}:
        a4 += ["Версия пакета: " + root.name + ".", ""]
    a4 += ["| Навык | Назначение из SKILL.md | Вход |", "| --- | --- | --- |"]
    for s in entries:
        a4.append("| " + link(s["name"], s["path"]) + " | " + cell(s["description"]) + " | " + s["entry"] + " |")
    a4.append("")

a5 = [
    "# A5 — Полный указатель действующих инструкций",
    "",
    "A5 нужен, когда неизвестно, где описана нужная возможность. Найди модуль, имя навыка, инструмент или тему, затем открой соответствующий исходный файл. Выбирай нужные инструкции по задаче; короткий список навыков находится в [A4](A4.md).",
    "",
    "Срез: "
    + DAY
    + ". Всего "
    + str(len(documents))
    + " файлов: "
    + str(project_count)
    + " в AutoStop Manager и "
    + str(plugin_count)
    + " во встроенных навыках и текущих пакетах Codex.",
    "",
    "Включены действующие общие правила, все SKILL.md выбранных текущих пакетов, дополнительные текстовые инструкции, справочники, шаблоны и стартовые запросы навыков. Область проекта — AGENTS.md, .agents/skills/ и docs/. Область Codex — встроенные навыки и текущие установленные пакеты; доступность их инструментов зависит от сеанса.",
    "",
    "Черновики, исторические отчёты аудита и релизов, архивы, лицензии и старые версии пакетов исключены. Инструкции CRM, Store и других самостоятельных проектов ищи по ссылкам соответствующих руководств Manager; их репозитории не входят в этот указатель.",
    "",
    "Для обновления A4/A5 запусти `python scripts/update-instruction-catalogs.py` из корня проекта. Скрипт меняет только каталоги Manager. Если в кэше несколько версий одного пакета, сначала установи текущую версию по окружению Codex. Доступность действий проверяй по текущим инструментам.",
    "",
    "## Источники",
    "",
    "| Набор | Версия / область | Файлов |",
    "| --- | --- | --- |",
]
for group, root in ROOTS:
    count = sum(d["group"] == group for d in documents)
    if not count:
        continue
    scope = (
        "AGENTS.md, docs/, .agents/skills/"
        if group == "AutoStop Manager"
        else "Встроенные навыки"
        if group == "Codex"
        else root.name
    )
    a5.append("| " + group + " | " + scope + " | " + str(count) + " |")
a5.append("")
for group, _root in ROOTS:
    if not any(d["group"] == group for d in documents):
        continue
    a5 += ["## " + group, "", "| Файл | Инструкция / материал | Тип |", "| --- | --- | --- |"]
    for d in (d for d in documents if d["group"] == group):
        relative = str(d["path"].relative_to(PROJECT)) if group == "AutoStop Manager" else d["relative"]
        a5.append("| " + link(relative, d["path"]) + " | " + cell(d["title"]) + " | " + d["kind"] + " |")
    a5.append("")

OUT.mkdir(exist_ok=True)
(OUT / "A4.md").write_text("\n".join(a4).rstrip() + "\n", encoding="utf-8")
(OUT / "A5.md").write_text("\n".join(a5).rstrip() + "\n", encoding="utf-8")
snapshot = {
    "skills": [{**s, "path": str(s["path"])} for s in skills],
    "documents": [{**d, "path": str(d["path"])} for d in documents],
}
Path("/tmp/autostop-instruction-catalog-inventory.json").write_text(
    json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8"
)
print(
    json.dumps(
        {
            "skills": len(skills),
            "main_skills": main_count,
            "nested_skills": sum(s["entry"] == "Вложенный" for s in skills),
            "installed_skills": sum(s["entry"] == "Установленный" for s in skills),
            "documents": len(documents),
            "project_documents": project_count,
            "codex_documents": plugin_count,
        },
        ensure_ascii=False,
    )
)
