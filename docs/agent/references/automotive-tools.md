# Единый автомобильный каталог

Registry: docs/agent/automotive_tools.json; JSON Schema: automotive_tools.schema.json.
Модули имеют устойчивый module_key, инструменты — tool_id. E-code отображает задачу.
Markdown является текстом правил; inputSchema авторитетна в code/MCP.
Карточки генерирует scripts/generate-automotive-instructions.py; --check проверяет отсутствие drift.
Все actual native names учтены, 43 PartsAPI API-methods сопоставлены exact operation;
outside/historical/diagnostic entries явно отделены от активных автомобильных операций.

## Проверка и экспорт

В isolated source worktree с отдельной venv и disposable state:

```bash
.venv/bin/python scripts/generate-automotive-instructions.py --check
.venv/bin/python scripts/check-automotive-catalog.py
.venv/bin/python scripts/update-instruction-catalogs.py --check
./scripts/release-gates.sh
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
Поставить export после checked Manager publication, повторить CRM local gates и exact merged CI.
Открытие окна не обращается к providers и не читает Manager checkout.
Coordinated deploy сравнивает installed Manager REVISION и CRM pin/hash.
Порядок schema→Manager producer→CRM pinned consumer; Store API не меняется.

## Статусы и права

Manual states: not_commissioned/temporarily_unavailable/working; absence отображается красным без записи GET.
Shared tool_id имеет одну запись tool_statuses. server назначает actor/time; owner/CAS/idempotency обязательны.
set_tool_status меняет ровно одну запись, clear_tool_status возвращает прежнее отсутствие для технического smoke.
Ни один цвет не запускает provider и не заменяет result evidence.
Полный release backup сохраняет statuses; portable graph template их не переносит.
Graph replace/layout/upsert/reroute сохраняют map, новая catalog revision не стирает старые IDs.

## Миграция и выпуск

CRM scripts/migrate_e1_structure.py готовит graph из полного fresh private snapshot и exact bundle.
Не-E elements/relations/canvas сохраняются; E relations remap перечислены в migration receipt.
Conflict409 требует нового snapshot/preview и rebuild; устаревший replace не повторяется.
Preview не меняет persisted state, apply выполняется guarded CRM API/Gateway, затем полный readback.
Release/rollback — [deployment](deployment.md); current/previous coherent tuple и business data сохраняются.
После открытия live writers нельзя вернуть старую DB ради отката UI.

## Контракт данных

Новые tools возвращают outcome/data/evidence/missing_fields/conflicts/warnings/execution.
input_binding является односторонним SHA(kind:normalized_identifier), не восстановлением VIN.
Reuse проверяет binding/kind и relevant engine/KPP/date/market контекст до provider call.
Source scope/primary lineage сохраняются; copied data не создают вторую независимую базу.
Namespace+entity_kind+id+parent/tree binding обязательны для точного catalog reference.
Pure helpers не читают сеть/CRM/experience; readiness и результат каждой операции различны.
