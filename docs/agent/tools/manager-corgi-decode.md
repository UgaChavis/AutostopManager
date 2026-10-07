# manager.corgi_decode — Локальная подготовленная vPIC DB Corgi

Локальная подготовленная vPIC DB Corgi

Основной модуль: [E2](../modules/E2.md); другие модули: нет.
Классификация: active. Состояние реализации: implemented.
Источник: corgi; первичная база: NHTSA vPIC DB via Corgi; same lineage as online vPIC. Исполнение: local_read.

Вызов: native Manager MCP `corgi_decode`.

Входы: `identifier`, `model_year`, `timeout_seconds`.
Defaults: `{"model_year":null,"timeout_seconds":10}`.
Обязательные facade поля: `identifier`.
Fingerprint проверяет [manifest](../manager_mcp_catalog.json); полная inputSchema берётся из регистрации и `tools/list`, в CRM — native_schemas того же bundle.

Синтетический вход (форма, не утверждение о реальном автомобиле/артикуле):
```json
{"identifier":"5YJSA1E26HF000001"}
```
Вход, отклоняемый схемой до исполнения инструмента:
```json
{"identifier":null}
```

Выход: Envelope: schema_version/tool_id/ok/outcome/data/evidence/missing_fields/conflicts/warnings/execution; paths ниже относятся к data.; vehicle_profile/decoder_valid/decoder_errors/checksum/model_year_source/input_binding/field_provenance/database{version,sha256}; evidence.primary_lineage=nhtsa_vpic; alternatives этот инструмент не строит.
Ошибки и неполнота: invalid_input: полный VIN, model_year integer1980..2100, timeout_seconds в (0,30]; dependency_missing/database_missing/configuration_missing; parse_error с warning причины/таймаута; partial при make/manufacturer, unsupported без них.

- Модельный год отдельно от даты производства; WMI не определяет двигатель.
- Corgi и vPIC имеют одну первичную базу; два результата не два независимых свидетельства.
- Attested локальная vPIC DB и worker без download/fallback; network_calls=0.
- Переданный model_year имеет provenance explicit_model_year; Corgi и online vPIC — одна первичная база.

Подробный контракт: [справочник](../references/automotive-offline.md). Карточка и inputSchema согласованы versioned export; ручной цвет не является результатом проверки.
