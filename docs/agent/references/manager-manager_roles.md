# manager.manager_roles — manager_roles

Ручной реестр задач, вопросов, согласований и знаний M1/M2

Основной модуль: outside; другие модули: нет.
Классификация: outside. Состояние реализации: implemented.
Источник: manager; первичная база: Manager role tables in the existing scheduler registry; exact source references. Исполнение: composed.

Вызов: native Manager MCP `manager_roles`.

Входы: `operation`, `role`, `reference_id`, `limit`.
Defaults: `{"limit":30,"operation":"status","reference_id":"","role":"M1"}`.
Обязательные facade поля: нет; ограничения конкретной операции всё равно применяются.
Текущая inputSchema и проверка версии — [D1](../modules/D1.md).

Синтетический вход (форма, не утверждение о реальном автомобиле/артикуле):
```json
{}
```
Вход, отклоняемый схемой до исполнения инструмента:
```json
{"operation":"__invalid__"}
```

Выход: ok и roles для status, items для списков и отдельных записей, receipt для owner_receipt.
Ошибки и неполнота: role_read_invalid при недопустимом запросе, role_item_not_found при отсутствии записи, automation_control_unavailable при недоступном контроллере.

- Чтение не создаёт хранилище реестра.

Подробный контракт: [справочник](manager-roles.md).
