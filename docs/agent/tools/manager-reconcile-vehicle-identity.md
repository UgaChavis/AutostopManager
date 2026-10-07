# manager.reconcile_vehicle_identity — Сверить переданные результаты

Сверить переданные результаты

Основной модуль: [E2](../modules/E2.md); другие модули: нет.
Классификация: active. Состояние реализации: implemented.
Источник: manager; первичная база: AutoStop versioned rules or explicit supplied inputs. Исполнение: pure.

Вызов: native Manager MCP `reconcile_vehicle_identity`.

Входы: `identifier`, `results`, `context`, `identifier_type`.
Defaults: `{"context":null,"identifier_type":"auto"}`.
Обязательные facade поля: `identifier`, `results`.
Fingerprint проверяет [manifest](../manager_mcp_catalog.json); полная inputSchema берётся из регистрации и `tools/list`, в CRM — native_schemas того же bundle.

Синтетический вход (форма, не утверждение о реальном автомобиле/артикуле):
```json
{"identifier":"WVWZZZ1KZAW000001","results":[{"evidence":[{"declares_oem":true,"document_kind":"official_epc","fetched_at":"2026-01-01T00:00:00Z","locator":"https://example.com/oem","method":"document_read","primary_lineage":"DEMO official OEM document","provider":"manufacturer","scope":"family","version":"demo-v1"}],"input_binding":{"identifier_kind":"vin","identifier_sha256":"0702c3dcffd51cfab2e949d7b8b634ae759219e92e7e4ee7549ea2cf9671ce5b","version":1},"vehicle_profile":{"make":"Volkswagen"}}]}
```
Вход, отклоняемый схемой до исполнения инструмента:
```json
{"identifier":null,"results":[{"evidence":[{"declares_oem":true,"document_kind":"official_epc","fetched_at":"2026-01-01T00:00:00Z","locator":"https://example.com/oem","method":"document_read","primary_lineage":"DEMO official OEM document","provider":"manufacturer","scope":"family","version":"demo-v1"}],"input_binding":{"identifier_kind":"vin","identifier_sha256":"0702c3dcffd51cfab2e949d7b8b634ae759219e92e7e4ee7549ea2cf9671ce5b","version":1},"vehicle_profile":{"make":"Volkswagen"}}]}
```

Выход: Envelope: schema_version/tool_id/ok/outcome/data/evidence/missing_fields/conflicts/warnings/execution; paths ниже относятся к data.; vehicle_profile/field_statuses(observed или disputed)/provenance/variants/model_year_candidates/family_candidates; input_binding/conflicts/missing_fields/parts_lookup_readiness; спорное поле исключается из profile.
Ошибки и неполнота: invalid_input для неверной формы results/evidence/alternatives; partial при missing/conflicts; failed outcome, provider mismatch и upstream conflicts не становятся наблюдаемыми фактами.

- Модельный год отдельно от даты производства; WMI не определяет двигатель.
- До500 переданных результатов, network_calls=0; input_binding должен совпадать с identifier (WMI ограничен manufacturer-family).
- Сохраняются child variants/model_year_candidates и исходная lineage, включая профиль провайдера, переданный как context.

Подробный контракт: [справочник](../references/vehicle-identity.md). Карточка и inputSchema согласованы versioned export; ручной цвет не является результатом проверки.
