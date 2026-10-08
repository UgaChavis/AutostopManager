# manager.decode_vin_vpic — Только VIN endpoint vPIC

Основной модуль: [E2](../modules/E2.md); другие модули: нет.
Классификация: active. Состояние реализации: implemented.
Источник: vpic; первичная база: NHTSA manufacturer-reported vPIC. Исполнение: network_read.

Вызов: native Manager MCP `decode_vin_vpic`.

Входы: `identifier`, `model_year`, `timeout_seconds`, `extended`.
Defaults: `{"extended":false,"model_year":null,"timeout_seconds":8}`.
Обязательные facade поля: `identifier`.
Текущая inputSchema и проверка версии — [D1](../modules/D1.md).

Синтетический вход (форма, не утверждение о реальном автомобиле/артикуле):
```json
{"identifier":"WVWZZZ1KZAW000001","timeout_seconds":8}
```
Вход, отклоняемый схемой до исполнения инструмента:
```json
{"identifier":null,"timeout_seconds":8}
```

Выход: Envelope: schema_version/tool_id/ok/outcome/data/evidence/missing_fields/conflicts/warnings/execution; paths ниже относятся к data.; vehicle_profile/input_binding/wmi(null)/identifier_binding/diagnostics; evidence содержит provider/primary_lineage/method/fetched_at/locator/scope; alternatives и field provenance этим инструментом не строятся.
Ошибки и неполнота: invalid_input до сети: требуется полный VIN и timeout_seconds в [0.1,8]; empty/partial/parse_error/provider_error сохраняются; mismatch/unverified binding не становится точной идентичностью.

- Модельный год отдельно от даты производства; WMI не определяет двигатель.
- Corgi и vPIC имеют одну первичную базу; два результата не два независимых свидетельства.
- Один выбранный VIN endpoint; extended явно выбирает расширенный endpoint. WMI/PartsAPI fallback отсутствует.
- model_year — целое 1900..следующий год UTC; это подсказка, не дата производства.

Подробный контракт: [справочник](../references/vehicle-identity.md).
