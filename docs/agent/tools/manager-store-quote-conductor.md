# manager.store_quote_conductor — store_quote_conductor

Справочный/отдельный контур; не automotive workflow

Основной модуль: outside; другие модули: нет.
Классификация: outside. Состояние реализации: implemented.
Источник: manager; первичная база: AutoStop versioned rules or explicit supplied inputs. Исполнение: business_write.

Вызов: native Manager MCP `store_quote_conductor`.

Входы: `operation`, `quote_request_id`, `run_id`, `expected_state_version`, `expected_revision`, `idempotency_key`, `correlation_id`, `entries`, `coverage`, `customer_response`, `evidence`, `consent_context_hash`, `published_snapshot_hash`, `mode`.
Defaults: `{"consent_context_hash":"","correlation_id":"","coverage":null,"customer_response":"","entries":null,"evidence":null,"expected_revision":"","expected_state_version":null,"idempotency_key":"","mode":"apply","published_snapshot_hash":"","quote_request_id":"","run_id":null}`.
Обязательные facade поля: `operation`.
Fingerprint проверяет [manifest](../manager_mcp_catalog.json); полная inputSchema берётся из регистрации и `tools/list`, в CRM — native_schemas того же bundle.

Синтетический вход (форма, не утверждение о реальном автомобиле/артикуле):
```json
{"operation":"status"}
```
Вход, отклоняемый схемой до исполнения инструмента:
```json
{"operation":[]}
```

Выход: outcome/data/evidence/missing_fields/conflicts/warnings/execution (новые helpers); legacy поля сохраняются.
Ошибки и неполнота: invalid_input сохраняет ошибочный вход как отказ; empty/partial/unsupported не success; configuration/auth/quota/provider/parse ошибки различаются для сетевого источника.

- Назначение и полномочия определяет профильный контур; automotive smoke не выполняет записи.
- Наличие в общем native inventory не разрешает business action или чтение клиента.

Подробный контракт: [справочник](../references/manager-runtime.md). Карточка и inputSchema согласованы versioned export; ручной цвет не является результатом проверки.
