# manager.decode_vehicle_batch — Список через один выбранный decoder

Список через один выбранный decoder

Основной модуль: [E2](../modules/E2.md); другие модули: нет.
Классификация: active. Состояние реализации: implemented.
Источник: manager; первичная база: AutoStop versioned rules or explicit supplied inputs. Исполнение: composed.

Вызов: native Manager MCP `decode_vehicle_batch`.

Входы: `items`, `decoder`, `deadline_seconds`.
Defaults: `{"deadline_seconds":30}`.
Обязательные facade поля: `items`, `decoder`.
Fingerprint проверяет [manifest](../manager_mcp_catalog.json); полная inputSchema берётся из регистрации и `tools/list`, в CRM — native_schemas того же bundle.

Синтетический вход (форма, не утверждение о реальном автомобиле/артикуле):
```json
{"decoder":"decode_wmi_local","items":["WVW","ZZZ"]}
```
Вход, отклоняемый схемой до исполнения инструмента:
```json
{"decoder":"decode_wmi_local","items":null}
```

Выход: Envelope: schema_version/tool_id/ok/outcome/data/evidence/missing_fields/conflicts/warnings/execution; paths ниже относятся к data.; decoder/items[{item_index,result}]/deadline_seconds; порядок и число строк сохраняются; execution суммирует network_calls/attempts дочернего выбранного decoder.
Ошибки и неполнота: invalid_input для decoder/items/deadline; отдельная плохая строка получает invalid_input/provider_error; deadline_exceeded сохраняется на строках; общий outcome partial при любой неполной строке.

- Модельный год отдельно от даты производства; WMI не определяет двигатель.
- decoder: decode_vin_vpic/decode_wmi_vpic/decode_wmi_local/decode_frame_local/vin_brand_details/vininfo_decode/corgi_decode. До500 строк, deadline_seconds в [0.1,120].
- Один явно выбранный decoder, без fallback. Network только для vPIC. Отмена останавливает следующие строки; уже выданный sync read ограничен transport timeout.

Подробный контракт: [справочник](../references/vehicle-identity.md). Карточка и inputSchema согласованы versioned export; ручной цвет не является результатом проверки.
