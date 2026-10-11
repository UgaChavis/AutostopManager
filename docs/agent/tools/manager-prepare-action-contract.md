# manager.prepare_action_contract — prepare_action_contract

Справочный/отдельный контур; не automotive workflow

Основной модуль: outside; другие модули: нет.
Классификация: outside. Состояние реализации: implemented.
Источник: manager; первичная база: AutoStop versioned rules or explicit supplied inputs. Исполнение: local_read.

Вызов: native Manager MCP `prepare_action_contract`.

Входы: `domain`, `action`, `target_id`, `planned_changes`, `owner_intent`, `expected_revision`, `idempotency_key`, `correlation_id`, `run_id`, `actor`, `dry_run`.
Defaults: `{"actor":"codex-owner-agent","correlation_id":"","dry_run":true,"expected_revision":null,"idempotency_key":"","owner_intent":"","planned_changes":null,"run_id":null,"target_id":""}`.
Обязательные facade поля: `domain`, `action`.
Текущая inputSchema и проверка версии — [D1](../modules/D1.md).

Синтетический вход (форма, не утверждение о реальном автомобиле/артикуле):
```json
{"action":"DEMO","domain":"DEMO"}
```
Вход, отклоняемый схемой до исполнения инструмента:
```json
{"action":"DEMO","domain":null}
```

Выход: Flat ActionContractV2: ok/format=action_contract_v2/contract_id/correlation_id/run_id/domain/action/target/actor/owner_intent/dry_run/planned_changes/concurrency/idempotency/preflight/execution/verification/compensation/ledger/warnings.; preflight.blocking_reasons describes missing/unsupported inputs; execution.ready/tool/operation/gateway_arguments selects the executor. The contract builder does not perform or verify a business write.; Native Manager telemetry may additionally include top-level tool_execution; it is described by the shared Manager runtime contract..
Ошибки и неполнота: Blocked/unsupported/missing inputs return ok=false and preflight.blocking_reasons; warnings and execution.ready also describe executor or required-context availability. No generic invalid_input/outcome envelope is returned..

- Назначение и полномочия определяет профильный контур; automotive smoke не выполняет записи.
- Наличие в общем native inventory не разрешает business action или чтение клиента.

Подробный контракт: [справочник](../references/manager-runtime.md).
