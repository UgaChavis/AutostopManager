# manager.vin_brand_details — Локальные брендовые варианты

Локальные брендовые варианты

Основной модуль: E2; ссылки: нет. Состояние реализации: implemented.
Источник: manager; первичная база: AutoStop versioned rules or explicit supplied inputs. Исполнение: local_read.

Вызов: native Manager MCP `vin_brand_details`.

Входы: `identifier`, `context`.
Defaults: `{"context":null}`.
Обязательные facade поля: `identifier`.
Fingerprint проверяет [manifest](../manager_mcp_catalog.json); полная inputSchema берётся из регистрации и `tools/list`, в CRM — native_schemas того же bundle.

Синтетический вход (форма, не утверждение о реальном автомобиле/артикуле):
```json
{"context":{"make":"Audi"},"identifier":"WAUZZZ4H0AN000001"}
```
Отрицательный вход:
```json
{"context":{"market":"Europe"},"identifier":null}
```

Выход: Envelope: schema_version/tool_id/ok/outcome/data/evidence/missing_fields/conflicts/warnings/execution; paths ниже относятся к data.; input_binding/vehicle_profile/candidates[]/registry_version/field_provenance; conflicts возвращаются в envelope.
Ошибки и неполнота: invalid_input для неполного VIN или context не object; partial при поддержанном prefix, unsupported иначе; несовместимый context сохраняет conflicts.

- Модельный год отдельно от даты производства; WMI не определяет двигатель.
- Ограниченные VAG family rules Audi/Volkswagen/Skoda; поле model становится model_family, engine/options/date остаются неизвестными.

Подробный контракт: [справочник](../references/automotive-offline.md). Карточка и inputSchema согласованы versioned export; ручной цвет не является результатом проверки.
