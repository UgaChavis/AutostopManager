# manager.capture_oem_evidence — Типизированное OEM свидетельство

Типизированное OEM свидетельство

Основной модуль: E4; ссылки: нет. Состояние реализации: implemented.
Источник: manager; первичная база: AutoStop versioned rules or explicit supplied inputs. Исполнение: pure.

Вызов: native Manager MCP `capture_oem_evidence`.

Входы: `part_number`, `source`, `scope`, `vehicle_binding`, `brand`.
Defaults: `{"brand":null,"vehicle_binding":null}`.
Обязательные facade поля: `part_number`, `source`, `scope`.
Fingerprint проверяет [manifest](../manager_mcp_catalog.json); полная inputSchema берётся из регистрации и `tools/list`, в CRM — native_schemas того же bundle.

Синтетический вход (форма, не утверждение о реальном автомобиле/артикуле):
```json
{"brand":"DEMO","part_number":"DEMO-OEM-001","scope":"family","source":{"brand":"DEMO","declares_oem":true,"document_kind":"official_epc","fetched_at":"2026-01-01T00:00:00Z","locator":"https://example.com/oem","method":"document_read","part_number":"DEMO-OEM-001","primary_lineage":"DEMO official OEM document","provider":"manufacturer","scope":"family","version":"demo-v1"}}
```
Отрицательный вход:
```json
{"part_number":null,"scope":"family","source":{"declares_oem":true,"document_kind":"official_epc","fetched_at":"2026-01-01T00:00:00Z","locator":"https://example.com/oem","method":"document_read","primary_lineage":"DEMO official OEM document","provider":"manufacturer","scope":"family","version":"demo-v1"}}
```

Выход: Envelope: schema_version/tool_id/ok/outcome/data/evidence/missing_fields/conflicts/warnings/execution; paths ниже относятся к data.; candidate{raw_number,normalized_number,brand,kind,scope,vehicle_binding,oem_confirmed,fitment_confirmed=false}.
Ошибки и неполнота: invalid_input для number/source/scope неверной формы; partial с missing_fields если нет связанного первичного OEM evidence; kind=unknown/oem_confirmed=false.

- Article/cross/OE-reference не подтверждают оригинальный номер конкретного VIN.
- Для подтверждения source: provider/primary_lineage/method/locator/fetched_at(valid ISO с timezone), matching part_number/brand/scope, declares_oem=true и document_kind official_epc/oem_catalog_document/physical_oe_marking.
- exact_identifier требует valid hash binding в vehicle_binding и source.identifier_binding; modification — typed TecDoc reference и source.catalog_ref. OEM confirmation отдельно от fitment.

Подробный контракт: [справочник](../references/partsapi.md). Карточка и inputSchema согласованы versioned export; ручной цвет не является результатом проверки.
