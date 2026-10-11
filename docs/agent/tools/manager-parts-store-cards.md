# manager.parts_store_cards — parts_store_cards

Справочный/отдельный контур; не automotive workflow

Основной модуль: outside; другие модули: нет.
Классификация: outside. Состояние реализации: implemented.
Источник: manager; первичная база: AutoStop CRM parts_store board cards via guarded CRM Gateway; Manager only routes allowed operations and verifies exact card readback. Исполнение: business_write.

Вызов: native Manager MCP `parts_store_cards`.

Входы: `operation`, `query`, `limit`, `card_id`, `title`, `vehicle`, `description`, `note`, `expected_updated_at`, `idempotency_key`.
Defaults: `{"card_id":"","description":"","expected_updated_at":"","idempotency_key":"","limit":30,"note":"","query":"","title":"","vehicle":""}`.
Обязательные facade поля: `operation`.
Текущая inputSchema и проверка версии — [D1](../modules/D1.md).

Синтетический вход (форма, не утверждение о реальном автомобиле/артикуле):
```json
{"operation":"list"}
```
Вход, отклоняемый схемой до исполнения инструмента:
```json
{"operation":"__invalid__"}
```

Выход: Flat result: ok/status/column. list returns items/has_more; get/create/append_note return card. Successful append of an existing exact note may return status=already_present.; Failures return error/outcome_uncertain; status is blocked or uncertain. Guarded write failures may also return gateway_warning.; Native Manager telemetry may additionally include top-level tool_execution; it is described by the shared Manager runtime contract..
Ошибки и неполнота: Rejected target/column/revision/idempotency/input or Gateway read failure returns ok=false with error/status=blocked/outcome_uncertain=false. An unverified/unknown write result may use status=uncertain/outcome_uncertain=true and gateway_warning; reconcile the exact card before any retry..

- Назначение и полномочия определяет профильный контур; automotive smoke не выполняет записи.
- Наличие в общем native inventory не разрешает business action или чтение клиента.

Подробный контракт: [справочник](../references/manager-runtime.md).
