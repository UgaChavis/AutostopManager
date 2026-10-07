# manager.compare_vehicle_modifications — Сравнить готовые модификации

Сравнить готовые модификации

Основной модуль: [E2](../modules/E2.md); другие модули: нет.
Классификация: active. Состояние реализации: implemented.
Источник: manager; первичная база: AutoStop versioned rules or explicit supplied inputs. Исполнение: pure.

Вызов: native Manager MCP `compare_vehicle_modifications`.

Входы: `context`, `candidates`.
Defaults: `{}`.
Обязательные facade поля: `context`, `candidates`.
Fingerprint проверяет [manifest](../manager_mcp_catalog.json); полная inputSchema берётся из регистрации и `tools/list`, в CRM — native_schemas того же bundle.

Синтетический вход (форма, не утверждение о реальном автомобиле/артикуле):
```json
{"candidates":[{"make":"DEMO","transmission":"manual"},{"make":"DEMO","transmission":"automatic"}],"context":{"make":"DEMO"}}
```
Вход, отклоняемый схемой до исполнения инструмента:
```json
{"candidates":[{"make":"DEMO","transmission":"manual"},{"make":"DEMO","transmission":"automatic"}],"context":null}
```

Выход: Envelope: schema_version/tool_id/ok/outcome/data/evidence/missing_fields/conflicts/warnings/execution; paths ниже относятся к data.; comparisons[{candidate,conflicts}]/alternatives/selected; missing_fields содержит дискриминаторы между альтернативами.
Ошибки и неполнота: invalid_input для context/candidates неверной формы или >500; partial при нескольких вариантах с недостающими условиями; конфликтующие candidates сохраняются в comparisons.

- Модельный год отдельно от даты производства; WMI не определяет двигатель.
- Pure: не запрашивает каталог и не подтверждает конкретный VIN; selected появляется только для одного оставшегося candidate.

Подробный контракт: [справочник](../references/vehicle-identity.md). Карточка и inputSchema согласованы versioned export; ручной цвет не является результатом проверки.
