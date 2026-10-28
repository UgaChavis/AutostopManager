# manager.capture_oem_evidence — Типизированное OEM свидетельство

Основной модуль: [E4](../modules/E4.md); другие модули: нет.
Классификация: active. Состояние реализации: implemented.
Источник: manager; первичная база: AutoStop versioned rules or explicit supplied inputs. Исполнение: pure.

Вызов: native Manager MCP `capture_oem_evidence`.

Входы: `part_number`, `source`, `scope`, `vehicle_binding`, `brand`.
Defaults: `{"brand":null,"vehicle_binding":null}`.
Обязательные facade поля: `part_number`, `source`, `scope`.
Текущая inputSchema и проверка версии — [D1](../modules/D1.md).

Синтетический вход (форма, не утверждение о реальном автомобиле/артикуле):
```json
{"brand":"DEMO","part_number":"DEMO-OEM-001","scope":"family","source":{"brand":"DEMO","declares_oem":true,"document_kind":"official_epc","fetched_at":"2026-01-01T00:00:00Z","locator":"https://example.com/oem","method":"document_read","part_number":"DEMO-OEM-001","primary_lineage":"DEMO official OEM document","provider":"manufacturer","scope":"family","version":"demo-v1"}}
```
Вход, отклоняемый схемой до исполнения инструмента:
```json
{"part_number":null,"scope":"family","source":{"declares_oem":true,"document_kind":"official_epc","fetched_at":"2026-01-01T00:00:00Z","locator":"https://example.com/oem","method":"document_read","primary_lineage":"DEMO official OEM document","provider":"manufacturer","scope":"family","version":"demo-v1"}}
```

Выход: Envelope: schema_version/tool_id/ok/outcome/data/evidence/missing_fields/conflicts/warnings/execution; paths ниже относятся к data.; candidate{raw_number,normalized_number,brand,kind,scope,vehicle_binding,caller_evidence_sufficient,conditional_on_caller_evidence,independent_source_verified,oem_confirmed,fitment_confirmed,exact_applicability_confirmed,ready_for_crm_writeback}. `oem_confirmed`, `fitment_confirmed`, `exact_applicability_confirmed`, `independent_source_verified` и `ready_for_crm_writeback` всегда false.
Ошибки и неполнота: invalid_input для number/source/scope неверной формы; partial с missing_fields если условных данных недостаточно, kind=unknown. При структурной достаточности success означает только завершённую условную проверку и kind=oem_candidate; источник не аутентифицирован.

- Article/cross/OE-reference не подтверждают оригинальный номер конкретного VIN.
- Для условной структурной достаточности: provider/primary_lineage/method/locator/fetched_at(valid ISO с timezone), matching part_number/brand/scope, declares_oem=true и document_kind official_epc/oem_catalog_document/physical_oe_marking. Текст source остаётся заявлением caller, а не независимой проверкой.
- exact_identifier requires matching hash binding in vehicle_binding and source.identifier_binding; this digest does not authenticate the source. modification requires the same valid catalog ref in vehicle_binding and source.catalog_ref: either typed TecDoc numeric ID/carType, or a native Elcats/Japancats/SsangYong provider/namespace/entry_id/vehicle_context ref. Preserve the native namespace; neither reference confirms OEM or fitment.
- A public catalog row is not primary OEM evidence. Native catalog refs identify the bound modification and do not themselves make a public provider official_epc; primary document/source metadata and all remaining evidence gates still apply.
- source.part_number array/object/boolean не связывается с одним номером и не конкатенируется; существующее преобразование numeric scalar, включая integer 1234, сохраняется.

Подробный контракт: [справочник](../references/partsapi.md).
