# manager.explain_partsapi_category_for_intent — explain_partsapi_category_for_intent

Справочный/отдельный контур; не automotive workflow

Основной модуль: outside; другие модули: нет.
Классификация: historical. Состояние реализации: implemented.
Источник: manager; первичная база: AutoStop versioned rules or explicit supplied inputs. Исполнение: local_read.

Вызов: native Manager MCP `explain_partsapi_category_for_intent`.

Входы: `intent_id`, `query`, `path`.
Defaults: `{"path":null,"query":null}`.
Обязательные facade поля: `intent_id`.
Текущая inputSchema и проверка версии — [D1](../modules/D1.md).

Синтетический вход (форма, не утверждение о реальном автомобиле/артикуле):
```json
{"intent_id":"DEMO"}
```
Вход, отклоняемый схемой до исполнения инструмента:
```json
{"intent_id":null}
```

Выход: Flat historical fixture explanation: ok/schema/version/path/query/intent_id/count/matches/missing/active_for_current_contract, plus selected_category/historical_match/category_unresolved/explanation.; active_for_current_contract=false keeps selected_category=null and category_unresolved=true even when historical_match is present; a historical numeric category does not select a current PartsAPI method.; Native Manager telemetry may additionally include top-level tool_execution; it is described by the shared Manager runtime contract..
Ошибки и неполнота: A missing/unreadable/invalid local fixture still returns ok=true, missing=true, count=0, no matches, historical_match=null and category_unresolved=true. An unknown intent may return missing=false with no matches; neither case is a live PartsAPI provider failure. No generic automotive outcome/data/evidence envelope is added..

- Назначение и полномочия определяет профильный контур; automotive smoke не выполняет записи.
- Исторический numeric category fixture не современное дерево TecDoc.

Подробный контракт: [справочник](../references/manager-runtime.md).
