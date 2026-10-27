# manager.manager_roles — manager_roles

Состояние кабинетов M1/M2, задачи, согласования и рабочие знания

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

Выход: ok and roles/items/receipt for reads; ok and role/item for an accepted action.
Ошибки и неполнота: role_revision_conflict, role_idempotency_conflict, role_owner_required, role_approval_stale; automation_control_unavailable.

- Изменение роли не заменяет разрешение владельца на финансовое действие, отправку сотруднику или выпуск.
- Рабочие знания и наблюдения не расширяют полномочия.
- Обе роли после установки OFF; чтение не запускает дежурство.

Подробный контракт: [справочник](manager-roles.md).
