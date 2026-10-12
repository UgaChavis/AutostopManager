# manager.store_quote_conductor — store_quote_conductor

Справочный/отдельный контур; не automotive workflow

Основной модуль: outside; другие модули: нет.
Классификация: outside. Состояние реализации: implemented.
Источник: store; первичная база: AutoStop Store Admin V2 quote draft/order snapshots and Owner API responses; Manager refs-only workflow ledger and guarded workflow rules. Исполнение: business_write.

Вызов: native Manager MCP `store_quote_conductor`.

Входы: `operation`, `quote_request_id`, `run_id`, `expected_state_version`, `expected_revision`, `idempotency_key`, `correlation_id`, `entries`, `coverage`, `customer_response`, `evidence`, `consent_context_hash`, `published_snapshot_hash`, `mode`.
Defaults: `{"consent_context_hash":"","correlation_id":"","coverage":null,"customer_response":"","entries":null,"evidence":null,"expected_revision":"","expected_state_version":null,"idempotency_key":"","mode":"apply","published_snapshot_hash":"","quote_request_id":"","run_id":null}`.
Обязательные facade поля: `operation`.
Текущая inputSchema и проверка версии — [D1](../modules/D1.md).

Синтетический вход (форма, не утверждение о реальном автомобиле/артикуле):
```json
{"operation":"status"}
```
Вход, отклоняемый схемой до исполнения инструмента:
```json
{"operation":[]}
```

Выход: Agent envelope format=agent_envelope_v2: ok/run_id/status/summary/changes/verification/warnings/next_actions/page/meta. meta.conductor_format=store_quote_conductor_v1 identifies this workflow.; summary.workflow/phase/state_version/workflow_status carry compact ledger state; failures use summary.error_code/warnings, and verified operation-specific technical review may appear in summary. No generic helper outcome/data/evidence fields are added.; Native Manager telemetry may additionally include top-level tool_execution; it is described by the shared Manager runtime contract..
Ошибки и неполнота: Invalid operation/run/binding/state/consent/revision or Store execution refusal returns agent_envelope_v2 with ok=false, summary.error_code and warnings; status may be blocked/conflict/compensating. A completed handoff may instead return ok=true/status=handoff with summary.error_code identifying its reason and next_actions requesting human review; this does not confirm a quote/order/payment. No generic invalid_input/outcome wrapper is added..

- Назначение и полномочия определяет профильный контур; automotive smoke не выполняет записи.
- Наличие в общем native inventory не разрешает business action или чтение клиента.

Подробный контракт: [справочник](../references/manager-runtime.md).
