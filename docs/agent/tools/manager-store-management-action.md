# manager.store_management_action — store_management_action

Справочный/отдельный контур; не automotive workflow

Основной модуль: outside; другие модули: нет.
Классификация: outside. Состояние реализации: implemented.
Источник: manager; первичная база: AutoStop versioned rules or explicit supplied inputs. Исполнение: business_write.

Вызов: native Manager MCP `store_management_action`.

Входы: `domain`, `action`, `target_id`, `planned_changes`, `owner_intent`, `expected_updated_at`, `idempotency_key`, `correlation_id`, `mode`.
Defaults: `{"mode":"dry_run"}`.
Обязательные facade поля: `domain`, `action`, `target_id`, `planned_changes`, `owner_intent`, `expected_updated_at`, `idempotency_key`, `correlation_id`.
Fingerprint проверяет [manifest](../manager_mcp_catalog.json); полная inputSchema берётся из регистрации и `tools/list`, в CRM — native_schemas того же bundle.

Синтетический вход (форма, не утверждение о реальном автомобиле/артикуле):
```json
{"action":"DEMO","correlation_id":"DEMO","domain":"DEMO","expected_updated_at":"DEMO","idempotency_key":"DEMO","owner_intent":"DEMO","planned_changes":{},"target_id":"DEMO"}
```
Вход, отклоняемый схемой до исполнения инструмента:
```json
{"action":"DEMO","correlation_id":"DEMO","domain":null,"expected_updated_at":"DEMO","idempotency_key":"DEMO","owner_intent":"DEMO","planned_changes":{},"target_id":"DEMO"}
```

Выход: outcome/data/evidence/missing_fields/conflicts/warnings/execution (новые helpers); legacy поля сохраняются.
Ошибки и неполнота: invalid_input сохраняет ошибочный вход как отказ; empty/partial/unsupported не success; configuration/auth/quota/provider/parse ошибки различаются для сетевого источника.

- Назначение и полномочия определяет профильный контур; automotive smoke не выполняет записи.
- Наличие в общем native inventory не разрешает business action или чтение клиента.

Подробный контракт: [справочник](../references/manager-runtime.md). Карточка и inputSchema согласованы versioned export; ручной цвет не является результатом проверки.
