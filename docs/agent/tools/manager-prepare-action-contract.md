# manager.prepare_action_contract — prepare_action_contract

Справочный/отдельный контур; не automotive workflow

Основной модуль: outside; другие модули: нет.
Классификация: outside. Состояние реализации: implemented.
Источник: manager; первичная база: AutoStop versioned rules or explicit supplied inputs. Исполнение: local_read.

Вызов: native Manager MCP `prepare_action_contract`.

Входы: `domain`, `action`, `target_id`, `planned_changes`, `owner_intent`, `expected_revision`, `idempotency_key`, `correlation_id`, `run_id`, `actor`, `dry_run`.
Defaults: `{"actor":"codex-owner-agent","correlation_id":"","dry_run":true,"expected_revision":null,"idempotency_key":"","owner_intent":"","planned_changes":null,"run_id":null,"target_id":""}`.
Обязательные facade поля: `domain`, `action`.
Fingerprint проверяет [manifest](../manager_mcp_catalog.json); полная inputSchema берётся из регистрации и `tools/list`, в CRM — native_schemas того же bundle.

Синтетический вход (форма, не утверждение о реальном автомобиле/артикуле):
```json
{"action":"DEMO","domain":"DEMO"}
```
Вход, отклоняемый схемой до исполнения инструмента:
```json
{"action":"DEMO","domain":null}
```

Выход: outcome/data/evidence/missing_fields/conflicts/warnings/execution (новые helpers); legacy поля сохраняются.
Ошибки и неполнота: invalid_input сохраняет ошибочный вход как отказ; empty/partial/unsupported не success; configuration/auth/quota/provider/parse ошибки различаются для сетевого источника.

- Назначение и полномочия определяет профильный контур; automotive smoke не выполняет записи.
- Наличие в общем native inventory не разрешает business action или чтение клиента.

Подробный контракт: [справочник](../references/manager-runtime.md). Карточка и inputSchema согласованы versioned export; ручной цвет не является результатом проверки.
