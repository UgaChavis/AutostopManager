# manager.normalize_labor_time — Время и scope переданных работ

Время и scope переданных работ

Основной модуль: [E12](../modules/E12.md); другие модули: нет.
Классификация: active. Состояние реализации: implemented.
Источник: manager; первичная база: AutoStop versioned rules or explicit supplied inputs. Исполнение: pure.

Вызов: native Manager MCP `normalize_labor_time`.

Входы: `rows`, `unit`, `source`.
Defaults: `{"source":null,"unit":null}`.
Обязательные facade поля: `rows`.
Fingerprint проверяет [manifest](../manager_mcp_catalog.json); полная inputSchema берётся из регистрации и `tools/list`, в CRM — native_schemas того же bundle.

Синтетический вход (форма, не утверждение о реальном автомобиле/артикуле):
```json
{"rows":[{"workName":"DEMO работа","workTime":120}],"source":{"declares_oem":true,"document_kind":"official_epc","fetched_at":"2026-01-01T00:00:00Z","locator":"https://example.com/oem","method":"document_read","primary_lineage":"DEMO official OEM document","provider":"manufacturer","scope":"family","version":"demo-v1"},"unit":"minutes"}
```
Вход, отклоняемый схемой до исполнения инструмента:
```json
{"rows":null,"source":{"declares_oem":true,"document_kind":"official_epc","fetched_at":"2026-01-01T00:00:00Z","locator":"https://example.com/oem","method":"document_read","primary_lineage":"DEMO official OEM document","provider":"manufacturer","scope":"family","version":"demo-v1"},"unit":"minutes"}
```

Выход: Envelope: schema_version/tool_id/ok/outcome/data/evidence/missing_fields/conflicts/warnings/execution; paths ниже относятся к data.; labor[]: operation_id/operation_name/raw_time/raw_unit/hours/source/catalog_ref/scope/conditions/included_operations/overlaps_with/missing_fields; overlaps[] сохраняет included_operations и overlaps_with.
Ошибки и неполнота: invalid_input при malformed rows/source или >500; partial при отсутствующей операции, time/source или неподдержанной/неизвестной unit; hours остаетсяnull.

- Норматив не реальная длительность; overlap не считается дважды.
- workName/workTime и явные hours/labor_hours/norm_hours поддержаны; unit явно передается для workTime. Поддержаны hours/minutes/seconds и documented aliases.
- Pure: единица не угадывается, actual длительность не выдается за норматив; metadata overlap передается в calculation.

Подробный контракт: [справочник](../references/partsapi.md). Карточка и inputSchema согласованы versioned export; ручной цвет не является результатом проверки.
