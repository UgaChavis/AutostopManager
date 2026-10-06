# manager.decode_vehicle_identities — Составная расшифровка списка

Составная расшифровка списка

Основной модуль: E2; ссылки: нет. Состояние реализации: implemented.
Источник: manager; первичная база: AutoStop versioned rules or explicit supplied inputs. Исполнение: composed.

Вызов: native Manager MCP `decode_vehicle_identities`.

Входы: `items`, `live_vpic`, `use_vpic_batch`.
Defaults: `{"live_vpic":true,"use_vpic_batch":true}`.
Обязательные facade поля: `items`.
Fingerprint проверяет [manifest](../manager_mcp_catalog.json); полная inputSchema берётся из регистрации и `tools/list`, в CRM — native_schemas того же bundle.

Синтетический вход (форма, не утверждение о реальном автомобиле/артикуле):
```json
{"items":[{"identifier":"WVWZZZ1KZAW000001","make":"Volkswagen"}],"live_vpic":false}
```
Отрицательный вход:
```json
{"items":null}
```

Выход: Legacy flat batch: ok/status/count/success_count/error_count/partial_count; high_confidence_count/medium_confidence_count/low_confidence_count.; identity_coverage/vpic_batch/configured_paid_sources/missing_paid_sources/results[]; each result has the complete decode_vehicle_identity schema, including malformed rows.; Optional processing gives transport budget and cancellation metadata..
Ошибки и неполнота: Batch/row invalid_input is reported separately; one malformed row preserves healthy rows and order; per-row provider errors are retained..

- At most 500 input rows; legacy defaults live_vpic=true and use_vpic_batch=true can read vPIC.
- Local processing still reads versioned registry/configuration; input_binding is null for an invalid row.
- Batch success counts do not prove exact VIN fitment or a complete vehicle profile.

Подробный контракт: [справочник](../references/vehicle-identity.md). Карточка и inputSchema согласованы versioned export; ручной цвет не является результатом проверки.
