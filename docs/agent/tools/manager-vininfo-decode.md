# manager.vininfo_decode — Локальная структурная расшифровка VINinfo

Локальная структурная расшифровка VINinfo

Основной модуль: E2; ссылки: нет. Состояние реализации: implemented.
Источник: vininfo; первичная база: VINinfo BSD local structural and brand tables. Исполнение: local_read.

Вызов: native Manager MCP `vininfo_decode`.

Входы: `identifier`.
Defaults: `{}`.
Обязательные facade поля: `identifier`.
Fingerprint проверяет [manifest](../manager_mcp_catalog.json); полная inputSchema берётся из регистрации и `tools/list`, в CRM — native_schemas того же bundle.

Синтетический вход (форма, не утверждение о реальном автомобиле/артикуле):
```json
{"identifier":"WVWZZZ1KZAW000001"}
```
Отрицательный вход:
```json
{"identifier":null}
```

Выход: Envelope: schema_version/tool_id/ok/outcome/data/evidence/missing_fields/conflicts/warnings/execution; paths ниже относятся к data.; vehicle_profile/model_year_candidates/brand_details/checksum{matches,mandatory_for_all_markets}/input_binding/field_provenance; evidence: vininfo_local_rules, версия prepared runtime; дата производства не выводится из model_year_candidates.
Ошибки и неполнота: invalid_input для неполного VIN; dependency_missing/configuration_missing до worker; parse_error с warning причины/таймаута; partial при доступных manufacturer/model, unsupported без них.

- Модельный год отдельно от даты производства; WMI не определяет двигатель.
- Только подготовленный attested VINinfo runtime, network_calls=0; decode не устанавливает пакеты и не скачивает базы.

Подробный контракт: [справочник](../references/automotive-offline.md). Карточка и inputSchema согласованы versioned export; ручной цвет не является результатом проверки.
