# manager.store_management_action — store_management_action

Справочный/отдельный контур; не automotive workflow

Основной модуль: outside; другие модули: нет.
Классификация: outside. Состояние реализации: implemented.
Источник: store; первичная база: AutoStop Store scoped entities and guarded action responses (Store agent API); Manager ActionContract rules and exact preread/readback. Исполнение: business_write.

Вызов: native Manager MCP `store_management_action`.

Входы: `domain`, `action`, `target_id`, `planned_changes`, `owner_intent`, `expected_updated_at`, `idempotency_key`, `correlation_id`, `mode`.
Defaults: `{"mode":"dry_run"}`.
Обязательные facade поля: `domain`, `action`, `target_id`, `planned_changes`, `owner_intent`, `expected_updated_at`, `idempotency_key`, `correlation_id`.
Текущая inputSchema и проверка версии — [D1](../modules/D1.md).

Синтетический вход (форма, не утверждение о реальном автомобиле/артикуле):
```json
{"action":"DEMO","correlation_id":"DEMO","domain":"DEMO","expected_updated_at":"DEMO","idempotency_key":"DEMO","owner_intent":"DEMO","planned_changes":{},"target_id":"DEMO"}
```
Вход, отклоняемый схемой до исполнения инструмента:
```json
{"action":"DEMO","correlation_id":"DEMO","domain":null,"expected_updated_at":"DEMO","idempotency_key":"DEMO","owner_intent":"DEMO","planned_changes":{},"target_id":"DEMO"}
```

Выход: Store envelope format=store_agent_v1: ok/status/summary/items/changes/page/warnings/meta; fields supplied by the Store response are preserved. Blocked local failures may omit changes/generated_at.; summary.error_code carries local/provider failure. meta records contract_id/domain/action/mode/pre_state_read/pre_read_revision_mismatch/post_state_read/readback_verified; uncertain apply/readback failures return compensating status and summary.write_applied_unverified.; Native Manager telemetry may additionally include top-level tool_execution; it is described by the shared Manager runtime contract..
Ошибки и неполнота: Unsupported operations/modes, ActionContract/preflight/target/revision failures return a Store envelope with ok=false and summary.error_code; blocked/conflict statuses distinguish refusals. Apply with uncertain outcome or failed exact readback may return compensating; never infer a successful write from transport acceptance..

- Назначение и полномочия определяет профильный контур; automotive smoke не выполняет записи.
- Наличие в общем native inventory не разрешает business action или чтение клиента.

Подробный контракт: [справочник](../references/manager-runtime.md).
