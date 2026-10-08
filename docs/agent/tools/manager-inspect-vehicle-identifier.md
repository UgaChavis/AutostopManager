# manager.inspect_vehicle_identifier — Проверить формат identifier

Основной модуль: [E2](../modules/E2.md); другие модули: нет.
Классификация: active. Состояние реализации: implemented.
Источник: manager; первичная база: AutoStop versioned rules or explicit supplied inputs. Исполнение: pure.

Вызов: native Manager MCP `inspect_vehicle_identifier`.

Входы: `identifier`, `identifier_type`.
Defaults: `{"identifier_type":"auto"}`.
Обязательные facade поля: `identifier`.
Текущая inputSchema и проверка версии — [D1](../modules/D1.md).

Синтетический вход (форма, не утверждение о реальном автомобиле/артикуле):
```json
{"identifier":"WVWZZZ1KZAW000001"}
```
Вход, отклоняемый схемой до исполнения инструмента:
```json
{"identifier":null}
```

Выход: Envelope: schema_version/tool_id/ok/outcome/data/evidence/missing_fields/conflicts/warnings/execution; paths ниже относятся к data.; identifier_kind/valid/wmi/input_binding/format; исходный identifier в ответ не копируется.
Ошибки и неполнота: invalid_input при неверном типе или явном identifier_type, не совпадающем с форматом; unsupported для неизвестного формата.

- Модельный год отдельно от даты производства; WMI не определяет двигатель.
- identifier_type: auto/vin/vin_partial/frame_number/market_code; network_calls=0.

Подробный контракт: [справочник](../references/vehicle-identity.md).
