# manager.decode_wmi_vpic — Только WMI endpoint vPIC

Основной модуль: [E2](../modules/E2.md); другие модули: нет.
Классификация: active. Состояние реализации: implemented.
Источник: vpic; первичная база: NHTSA manufacturer-reported vPIC. Исполнение: network_read.

Вызов: native Manager MCP `decode_wmi_vpic`.

Входы: `wmi`, `timeout_seconds`.
Defaults: `{"timeout_seconds":8}`.
Обязательные facade поля: `wmi`.
Текущая inputSchema и проверка версии — [D1](../modules/D1.md).

Синтетический вход (форма, не утверждение о реальном автомобиле/артикуле):
```json
{"wmi":"WVW"}
```
Вход, отклоняемый схемой до исполнения инструмента:
```json
{"wmi":null}
```

Выход: Envelope: schema_version/tool_id/ok/outcome/data/evidence/missing_fields/conflicts/warnings/execution; paths ниже относятся к data.; vehicle_profile: manufacturer/make/country/vehicle_type; wmi/identifier_binding/diagnostics; input_binding=null; это данные производителя, а не связанный профиль автомобиля.
Ошибки и неполнота: invalid_input для WMI не длины3/6 или timeout_seconds вне [0.1,8]; empty/partial/parse_error/provider_error возвращаются без переключения decoder.

- Модельный год отдельно от даты производства; WMI не определяет двигатель.
- Один выбранный WMI endpoint; модель/двигатель не восстанавливаются из WMI.

Подробный контракт: [справочник](../references/vehicle-identity.md).
