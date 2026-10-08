# Единый автомобильный каталог

Registry: docs/agent/automotive_tools.json; JSON Schema: automotive_tools.schema.json.
Модули имеют устойчивый module_key, инструменты — tool_id. E-code отображает задачу.
Markdown является текстом правил; inputSchema авторитетна в code/MCP.
Карточки генерирует scripts/generate-automotive-instructions.py; --check проверяет отсутствие drift.
Ссылки и классификация карточек берутся из registry; модульные Markdown поддерживаются отдельно и проверяются по его названиям, reference и таблицам операций.
Все actual native names учтены, 43 PartsAPI API-methods сопоставлены exact operation;
outside/historical/diagnostic entries явно отделены от активных автомобильных операций.

Положительный синтетический пример показывает форму входа, а не доступность поставщика.
invalid_example обязан отвергаться facade inputSchema до исполнения; nullable defaults не являются отрицательным входом.
Проверка этих примеров использует JSON Schema без вызова tools/providers. Доменные отказы проверяют отдельные unit tests;
не запускай подряд примеры job_write/business_write или составных инструментов с acquisition defaults.

## Проверка и экспорт

В isolated source worktree с отдельной venv и disposable state:

```bash
.venv/bin/python scripts/generate-automotive-instructions.py --check
.venv/bin/python scripts/check-automotive-catalog.py
.venv/bin/python scripts/update-instruction-catalogs.py --check
```

После публикации Manager и подтверждённого exact target SHA:

```bash
.venv/bin/python scripts/export-automotive-tools.py --revision FULL_PUBLISHED_MANAGER_SHA --output /PRIVATE/catalog.json
```

FULL_PUBLISHED_MANAGER_SHA заменяется фактическим 40-hex SHA. Export читает git archive,
а не bytes dirty checkout. Signature registration выполняется на temporary SQLite без provider calls.
Bundle schema=autostop.automotive-tools.bundle.v1; source_revision отдельно от content_hash.
Hash — SHA256 canonical JSON excluding source_revision/content_hash (ensure_ascii=false,sorted keys,compact separators).
В bundle входят тексты/хеши, registry, native_schemas, origins и migration map.

CRM committed pin: src/minimal_kanban/web_app_assets/source/automotive_tool_catalog.json.
Добавить export после публикации Manager; объём проверок и выпуск определяются поручением и [deployment](deployment.md).
Открытие окна не обращается к providers и не читает Manager checkout.
Coordinated deploy сравнивает installed Manager REVISION и CRM pin/hash.
Порядок schema→Manager producer→CRM pinned consumer; Store API не меняется.

## Статусы и права

В viewmode клик/Enter/Space модуля открывает центральное окно с инструкцией и карточками.
В editmode сохраняются выбор/drag/resize/connect конструктора.
Manual states: not_commissioned/temporarily_unavailable/working; absence отображается красным без записи GET.
Shared tool_id имеет одну запись tool_statuses. server назначает actor/time; owner/CAS/idempotency обязательны.
set_tool_status меняет ровно одну запись, clear_tool_status возвращает прежнее отсутствие для технического smoke.
Ни один цвет не запускает provider и не заменяет result evidence.
Состояние хранит CRM manager_structure.json; смена цвета не включает источник.
Каталог и Markdown поставляются readonly bundle: окно не читает dirty checkout и не обращается к providers.
Source Git SHA и content hash раздельны; миграция E-кодов сохраняет tool_id и не переносит прежние indicators в ручные цвета.
Полный release backup сохраняет statuses; portable graph template их не переносит.
Graph replace/layout/upsert/reroute сохраняют map, новая catalog revision не стирает старые IDs.

## Согласование инструкций и выпуск

Исходные правила хранятся по прежним путям: AGENTS, модули, навыки, карточки и справочники.
A2 соответствует AGENTS.md, остальные узлы — модульным Markdown по ID.
Автомобильный bundle и сохранённые тексты конструктора обновляются из опубликованного источника.
Для длинного A5 сохраняй короткую ссылку на полный указатель без зафиксированных количества файлов и SHA.

CRM scripts/sync_manager_structure_instructions.py готовит тексты, шаблоны и preview
из технического снимка, Manager root и exact bundle; режим check выявляет расхождения.
Обновление инструкции сохраняет ID, название, parent, геометрию, связи и tool_statuses.
Существующий scripts/migrate_e1_structure.py предназначен только для старой карты;
при обновлении текста его не запускай: он перестраивает E-геометрию.
Preview не меняет live state; применение — через owner/CAS/idempotency API с независимым readback.
При конфликте409 перечитай полный граф и заново подготовь текстовые изменения.
Release/rollback — [deployment](deployment.md); current/previous coherent tuple и business data сохраняются.
После открытия live writers нельзя вернуть старую DB ради отката UI.

## Контракт данных

Общие поля результата, происхождение, границы применения и полномочия — [E1](../modules/E1.md).
input_binding — односторонний SHA(kind:normalized_identifier); в технические отчёты его не сохраняй.
Reuse проверяет binding/kind и relevant engine/KPP/date/market контекст до provider call.
