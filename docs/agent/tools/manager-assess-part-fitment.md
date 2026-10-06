# manager.assess_part_fitment — Применимость по готовым evidence

Применимость по готовым evidence

Основной модуль: E6; ссылки: нет. Состояние реализации: implemented.
Источник: manager; первичная база: AutoStop versioned rules or explicit supplied inputs. Исполнение: pure.

Вызов: native Manager MCP `assess_part_fitment`.

Входы: `vehicle`, `part`, `criteria`, `evidence`, `scope`.
Defaults: `{"scope":"modification"}`.
Обязательные facade поля: `vehicle`, `part`, `criteria`, `evidence`.
Fingerprint проверяет [manifest](../manager_mcp_catalog.json); полная inputSchema берётся из регистрации и `tools/list`, в CRM — native_schemas того же bundle.

Синтетический вход (форма, не утверждение о реальном автомобиле/артикуле):
```json
{"criteria":{"engine":"DEMO-ENGINE"},"evidence":[{"declares_oem":true,"document_kind":"official_epc","fetched_at":"2026-01-01T00:00:00Z","locator":"https://example.com/oem","method":"document_read","primary_lineage":"DEMO official OEM document","provider":"manufacturer","scope":"family","version":"demo-v1"}],"part":{"brand":"DEMO","number":"DEMO-ARTICLE-001"},"scope":"family","vehicle":{"vehicle_profile":{"make":"DEMO"}}}
```
Отрицательный вход:
```json
{"criteria":{"engine":"DEMO-ENGINE"},"evidence":[{"declares_oem":true,"document_kind":"official_epc","fetched_at":"2026-01-01T00:00:00Z","locator":"https://example.com/oem","method":"document_read","primary_lineage":"DEMO official OEM document","provider":"manufacturer","scope":"family","version":"demo-v1"}],"part":{"brand":"DEMO","number":"DEMO-ARTICLE-001"},"scope":"family","vehicle":null}
```

Выход: Envelope: schema_version/tool_id/ok/outcome/data/evidence/missing_fields/conflicts/warnings/execution; paths ниже относятся к data.; state=supported/unknown/conflict/rejected; scope/part/checked_conditions; Checked conditions объединяет caller criteria и restrictions bound primary evidence; conflicts/missing_fields остаются отдельно.
Ошибки и неполнота: invalid_input для vehicle/part/criteria/evidence/scope неверной формы; partial для unknown/conflict; supported/rejected имеют outcome success как завершённая оценка.

- Нет явного запрета не означает supported; family не exact_identifier.
- Primary source: fitment_assertion=true, official_epc/manufacturer_fitment_catalog, matching part_number/brand/scope и provider/lineage/method/locator/aware ISO fetched_at.
- exact_identifier требует matching valid input_binding; modification требует matching typed TecDoc catalog_ref (numeric ID/carType). evidence.conditions проверяются вместе с criteria; отсутствующее/невалидное условие не supported.
- production_date может быть YYYY-MM-DD/месяц YYYY-MM; частичный месяц, пересекающий границу диапазона, остаётся unknown.

Подробный контракт: [справочник](../references/partsapi.md). Карточка и inputSchema согласованы versioned export; ручной цвет не является результатом проверки.
