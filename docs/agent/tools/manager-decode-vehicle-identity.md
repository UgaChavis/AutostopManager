# manager.decode_vehicle_identity — Расшифровать автомобиль составным способом

Расшифровать автомобиль составным способом

Основной модуль: [E2](../modules/E2.md); другие модули: нет.
Классификация: active. Состояние реализации: implemented.
Источник: manager; первичная база: AutoStop versioned rules or explicit supplied inputs. Исполнение: composed.

Вызов: native Manager MCP `decode_vehicle_identity`.

Входы: `identifier`, `vehicle`, `make`, `model`, `model_year`, `engine`, `transmission`, `drivetrain`, `market`, `source_confidence`, `live_vpic`, `live_wmi`, `production_year`, `production_date`, `modification`, `trim`, `series`, `options`, `transmission_speeds`, `identifier_type`.
Defaults: `{"drivetrain":null,"engine":null,"identifier_type":"auto","live_vpic":true,"live_wmi":true,"make":null,"market":null,"model":null,"model_year":null,"modification":null,"options":null,"production_date":null,"production_year":null,"series":null,"source_confidence":null,"transmission":null,"transmission_speeds":null,"trim":null,"vehicle":null}`.
Обязательные facade поля: `identifier`.
Fingerprint проверяет [manifest](../manager_mcp_catalog.json); полная inputSchema берётся из регистрации и `tools/list`, в CRM — native_schemas того же bundle.

Синтетический вход (форма, не утверждение о реальном автомобиле/артикуле):
```json
{"identifier":"WVWZZZ1KZAW000001","live_vpic":false,"live_wmi":false}
```
Вход, отклоняемый схемой до исполнения инструмента:
```json
{"identifier":null}
```

Выход: Legacy flat object: ok/status/schema_version=2/input_binding; invalid rows have input_binding=null.; vehicle_profile, field_statuses, field_evidence, provenance, evidence_sources, family_candidates, missing_fields, conflicts, warnings, errors, provider_errors.; identifier/identifier_validation/privacy/normalization_notes; confidence/confidence_label/confidence_semantics; adapter_status/lookup_plan/registry_version/parts_lookup_readiness; optional processing..
Ошибки и неполнота: status=invalid_input and errors[] for malformed identifier, year or context; provider_errors[] records source failures; partial profile is separate from complete identity..

- live_vpic=true and live_wmi=true are legacy defaults and may perform provider reads; set both false for local-only identity inspection.
- WMI and platform family do not establish engine or exact modification; model year and production date are separate fields.
- Copied provider data retains provider lineage and is not independent CRM corroboration.

Подробный контракт: [справочник](../references/vehicle-identity.md). Карточка и inputSchema согласованы versioned export; ручной цвет не является результатом проверки.
