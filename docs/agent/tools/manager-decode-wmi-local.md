# manager.decode_wmi_local — Локальные WMI hints

Локальные WMI hints

Основной модуль: E2; ссылки: нет. Состояние реализации: implemented.
Источник: manager; первичная база: AutoStop versioned rules or explicit supplied inputs. Исполнение: local_read.

Вызов: native Manager MCP `decode_wmi_local`.

Входы: `wmi`.
Defaults: `{}`.
Обязательные facade поля: `wmi`.
Fingerprint проверяет [manifest](../manager_mcp_catalog.json); полная inputSchema берётся из регистрации и `tools/list`, в CRM — native_schemas того же bundle.

Синтетический вход (форма, не утверждение о реальном автомобиле/артикуле):
```json
{"wmi":"WVW"}
```
Отрицательный вход:
```json
{"wmi":null}
```

Выход: Envelope: schema_version/tool_id/ok/outcome/data/evidence/missing_fields/conflicts/warnings/execution; paths ниже относятся к data.; wmi/hints/vehicle_profile/field_provenance; profile ограничен make/manufacturer/country.
Ошибки и неполнота: invalid_input для WMI не длины3; partial при известном префиксе; unsupported при неизвестном.

- Модельный год отдельно от даты производства; WMI не определяет двигатель.
- Локальный versioned registry, network_calls=0; hints рынка/типа не являются фактами конкретного VIN.

Подробный контракт: [справочник](../references/automotive-offline.md). Карточка и inputSchema согласованы versioned export; ручной цвет не является результатом проверки.
