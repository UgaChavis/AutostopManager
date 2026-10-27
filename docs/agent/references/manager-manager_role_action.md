# manager.manager_role_action — manager_role_action

Управление дежурствами, поручениями M1/M2 и конкретными согласованиями владельца

Основной модуль: outside; другие модули: нет.
Классификация: outside. Состояние реализации: implemented.
Источник: manager; первичная база: Manager role tables in the existing scheduler registry; exact source references. Исполнение: composed.

Вызов: native Manager MCP `manager_role_action`.

Входы: `operation`, `role`, `actor`, `idempotency_key`, `payload`, `expected_revision`.
Defaults: `{"expected_revision":null,"payload":null}`.
Обязательные facade поля: `operation`, `role`, `actor`, `idempotency_key`.
Текущая inputSchema и проверка версии — [D1](../modules/D1.md).

Синтетический вход (форма, не утверждение о реальном автомобиле/артикуле):
```json
{"actor":"M1","idempotency_key":"synthetic-note-1","operation":"note","payload":{"kind":"idea","source_ref":"test:1","text":"Synthetic operational idea"},"role":"M1"}
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
