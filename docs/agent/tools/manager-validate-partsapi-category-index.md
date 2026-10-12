# manager.validate_partsapi_category_index — validate_partsapi_category_index

Справочный/отдельный контур; не automotive workflow

Основной модуль: outside; другие модули: нет.
Классификация: historical. Состояние реализации: implemented.
Источник: manager; первичная база: AutoStop versioned rules or explicit supplied inputs. Исполнение: local_read.

Вызов: native Manager MCP `validate_partsapi_category_index`.

Входы: `path`.
Defaults: `{"path":null}`.
Обязательные facade поля: нет; ограничения конкретной операции всё равно применяются.
Текущая inputSchema и проверка версии — [D1](../modules/D1.md).

Синтетический вход (форма, не утверждение о реальном автомобиле/артикуле):
```json
{}
```
Вход, отклоняемый схемой до исполнения инструмента:
```json
{"path":[]}
```

Выход: Flat historical fixture validation: ok/schema/version/path/category_count/errors/active_for_current_contract/privacy. errors contains position/code/cat_id validation entries.; ok=false for missing/unreadable/invalid fixture or invalid category rows; an empty errors list alone does not prove a fixture was read. active_for_current_contract=false.; Native Manager telemetry may additionally include top-level tool_execution; it is described by the shared Manager runtime contract..
Ошибки и неполнота: ok=false for missing/unreadable/invalid fixture or row-validation errors; errors entries use invalid_cat_id/duplicate_cat_id/missing_intent_ids/missing_names. Missing fixture can return errors=[]; inspect ok/category_count/path together..

- Назначение и полномочия определяет профильный контур; automotive smoke не выполняет записи.
- Исторический numeric category fixture не современное дерево TecDoc.

Подробный контракт: [справочник](../references/manager-runtime.md).
