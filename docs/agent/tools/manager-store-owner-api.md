# manager.store_owner_api — store_owner_api

Справочный/отдельный контур; не automotive workflow

Основной модуль: outside; другие модули: нет.
Классификация: outside. Состояние реализации: implemented.
Источник: store; первичная база: AutoStop Store authenticated OpenAPI/owner API, exact target revision and operation responses; Manager validates contract, dry-run proof and readback. Исполнение: business_write.

Вызов: native Manager MCP `store_owner_api`.

Входы: `operation_id`, `mode`, `target_id`, `path_parameters`, `query`, `body`, `form`, `files`, `owner_intent`, `idempotency_key`, `correlation_id`, `expected_revision`, `expected_contract_id`, `prepare_for_mode`, `dry_run_proof`, `allow_binary_response`.
Defaults: `{"allow_binary_response":false,"body":null,"correlation_id":"","dry_run_proof":null,"expected_contract_id":null,"expected_revision":null,"files":null,"form":null,"idempotency_key":"","mode":"dry_run","owner_intent":"","path_parameters":null,"prepare_for_mode":"dry_run","query":null,"target_id":""}`.
Обязательные facade поля: `operation_id`.
Текущая inputSchema и проверка версии — [D1](../modules/D1.md).

Синтетический вход (форма, не утверждение о реальном автомобиле/артикуле):
```json
{"operation_id":"DEMO"}
```
Вход, отклоняемый схемой до исполнения инструмента:
```json
{"operation_id":null}
```

Выход: Owner envelope format=autostop_store_owner_api_v1: ok/status/summary/meta/data_included; failures add error.code and may summary.error_code. Schema/prepare failures may return a smaller envelope.; prepare returns status=validated with summary.prepared_for_mode/request_dispatched=false and meta.domain_handler_executed=false; revision/read/dry_run/apply results preserve their operation-specific summary/data/meta, including contract and reread verification details.; Native Manager telemetry may additionally include top-level tool_execution; it is described by the shared Manager runtime contract..
Ошибки и неполнота: Configuration/contract/schema/target/mode/idempotency/revision/dry-run-proof/provider failures return ok=false with error.code, operation-specific status and sanitized metadata; summary.error_code is present in client error envelopes. prepare validates dispatch inputs but does not prove a live operation or successful reread..

- Назначение и полномочия определяет профильный контур; automotive smoke не выполняет записи.
- Наличие в общем native inventory не разрешает business action или чтение клиента.

Подробный контракт: [справочник](../references/manager-runtime.md).
