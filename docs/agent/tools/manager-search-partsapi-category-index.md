# manager.search_partsapi_category_index — search_partsapi_category_index

Справочный/отдельный контур; не automotive workflow

Основной модуль: outside; другие модули: нет.
Классификация: historical. Состояние реализации: implemented.
Источник: manager; первичная база: AutoStop versioned rules or explicit supplied inputs. Исполнение: local_read.

Вызов: native Manager MCP `search_partsapi_category_index`.

Входы: `query`, `intent_id`, `path`, `limit`.
Defaults: `{"intent_id":null,"limit":8,"path":null,"query":null}`.
Обязательные facade поля: нет; ограничения конкретной операции всё равно применяются.
Текущая inputSchema и проверка версии — [D1](../modules/D1.md).

Синтетический вход (форма, не утверждение о реальном автомобиле/артикуле):
```json
{}
```
Вход, отклоняемый схемой до исполнения инструмента:
```json
{"query":[]}
```

Выход: Flat historical fixture result: ok/schema/version/path/query/intent_id/count/matches/missing/active_for_current_contract. Each match includes category provenance, score and matched_by.; Missing/unreadable/invalid fixture is represented by missing=true and empty matches; this search still returns ok=true. active_for_current_contract=false: a historical match does not select a current PartsAPI method.; Native Manager telemetry may additionally include top-level tool_execution; it is described by the shared Manager runtime contract..
Ошибки и неполнота: A missing/unreadable/invalid local fixture yields ok=true, missing=true, count=0 and no matches; this is absence of historical evidence, not an empty/partial outcome field. No live PartsAPI provider is called..

- Назначение и полномочия определяет профильный контур; automotive smoke не выполняет записи.
- Исторический numeric category fixture не современное дерево TecDoc.

Подробный контракт: [справочник](../references/manager-runtime.md).
