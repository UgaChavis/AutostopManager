# manager.vin_brand_details — Локальные брендовые варианты

Основной модуль: [E2](../modules/E2.md); другие модули: нет.
Классификация: active. Состояние реализации: implemented.
Источник: manager; первичная база: AutoStop versioned rules or explicit supplied inputs. Исполнение: local_read.

Вызов: native Manager MCP `vin_brand_details`.

Входы: `identifier`, `context`.
Defaults: `{"context":null}`.
Обязательные facade поля: `identifier`.
Текущая inputSchema и проверка версии — [D1](../modules/D1.md).

Синтетический вход (форма, не утверждение о реальном автомобиле/артикуле):
```json
{"context":{"make":"Audi"},"identifier":"WAUZZZ4H0AN000001"}
```
Вход, отклоняемый схемой до исполнения инструмента:
```json
{"context":{"market":"Europe"},"identifier":null}
```

Выход: Envelope: schema_version/tool_id/ok/outcome/data/evidence/missing_fields/conflicts/warnings/execution; paths ниже относятся к data.; input_binding/vehicle_profile/candidates[]/registry_version/field_provenance; conflicts возвращаются в envelope.
Ошибки и неполнота: invalid_input для неполного VIN или context не object; partial при поддержанном prefix, unsupported иначе; несовместимый context сохраняет conflicts.

- Модельный год отдельно от даты производства; WMI не определяет двигатель.
- Ограниченные VAG family rules Audi/Volkswagen/Skoda; поле model становится model_family, engine/options/date остаются неизвестными.

Подробный контракт: [справочник](../references/automotive-offline.md).
