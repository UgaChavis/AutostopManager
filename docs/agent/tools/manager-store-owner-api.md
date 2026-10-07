# manager.store_owner_api — store_owner_api

Справочный/отдельный контур; не automotive workflow

Основной модуль: outside; другие модули: нет.
Классификация: outside. Состояние реализации: implemented.
Источник: manager; первичная база: AutoStop versioned rules or explicit supplied inputs. Исполнение: business_write.

Вызов: native Manager MCP `store_owner_api`.

Входы: `operation_id`, `mode`, `target_id`, `path_parameters`, `query`, `body`, `form`, `files`, `owner_intent`, `idempotency_key`, `correlation_id`, `expected_revision`, `expected_contract_id`, `prepare_for_mode`, `dry_run_proof`, `allow_binary_response`.
Defaults: `{"allow_binary_response":false,"body":null,"correlation_id":"","dry_run_proof":null,"expected_contract_id":null,"expected_revision":null,"files":null,"form":null,"idempotency_key":"","mode":"dry_run","owner_intent":"","path_parameters":null,"prepare_for_mode":"dry_run","query":null,"target_id":""}`.
Обязательные facade поля: `operation_id`.
Fingerprint проверяет [manifest](../manager_mcp_catalog.json); полная inputSchema берётся из регистрации и `tools/list`, в CRM — native_schemas того же bundle.

Синтетический вход (форма, не утверждение о реальном автомобиле/артикуле):
```json
{"operation_id":"DEMO"}
```
Вход, отклоняемый схемой до исполнения инструмента:
```json
{"operation_id":null}
```

Выход: outcome/data/evidence/missing_fields/conflicts/warnings/execution (новые helpers); legacy поля сохраняются.
Ошибки и неполнота: invalid_input сохраняет ошибочный вход как отказ; empty/partial/unsupported не success; configuration/auth/quota/provider/parse ошибки различаются для сетевого источника.

- Назначение и полномочия определяет профильный контур; automotive smoke не выполняет записи.
- Наличие в общем native inventory не разрешает business action или чтение клиента.

Подробный контракт: [справочник](../references/manager-runtime.md). Карточка и inputSchema согласованы versioned export; ручной цвет не является результатом проверки.
