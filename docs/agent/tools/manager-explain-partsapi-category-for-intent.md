# manager.explain_partsapi_category_for_intent — explain_partsapi_category_for_intent

Справочный/отдельный контур; не automotive workflow

Основной модуль: outside; другие модули: нет.
Классификация: historical. Состояние реализации: implemented.
Источник: manager; первичная база: AutoStop versioned rules or explicit supplied inputs. Исполнение: local_read.

Вызов: native Manager MCP `explain_partsapi_category_for_intent`.

Входы: `intent_id`, `query`, `path`.
Defaults: `{"path":null,"query":null}`.
Обязательные facade поля: `intent_id`.
Fingerprint проверяет [manifest](../manager_mcp_catalog.json); полная inputSchema берётся из регистрации и `tools/list`, в CRM — native_schemas того же bundle.

Синтетический вход (форма, не утверждение о реальном автомобиле/артикуле):
```json
{"intent_id":"DEMO"}
```
Вход, отклоняемый схемой до исполнения инструмента:
```json
{"intent_id":null}
```

Выход: outcome/data/evidence/missing_fields/conflicts/warnings/execution (новые helpers); legacy поля сохраняются.
Ошибки и неполнота: invalid_input сохраняет ошибочный вход как отказ; empty/partial/unsupported не success; configuration/auth/quota/provider/parse ошибки различаются для сетевого источника.

- Назначение и полномочия определяет профильный контур; automotive smoke не выполняет записи.
- Исторический numeric category fixture не современное дерево TecDoc.

Подробный контракт: [справочник](../references/manager-runtime.md). Карточка и inputSchema согласованы versioned export; ручной цвет не является результатом проверки.
