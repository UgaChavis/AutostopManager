# manager.plan_oem_parts_providers — Необязательный несетевой план источников

Необязательный несетевой план источников

Основной модуль: [E4](../modules/E4.md); другие модули: нет.
Классификация: active. Состояние реализации: implemented.
Источник: manager; первичная база: AutoStop versioned rules or explicit supplied inputs. Исполнение: local_read.

Вызов: native Manager MCP `plan_oem_parts_providers`.

Входы: `identifier`, `requested_part`, `vehicle_identity`, `city`.
Defaults: `{"city":"Красноярск","vehicle_identity":null}`.
Обязательные facade поля: `identifier`, `requested_part`.
Fingerprint проверяет [manifest](../manager_mcp_catalog.json); полная inputSchema берётся из регистрации и `tools/list`, в CRM — native_schemas того же bundle.

Синтетический вход (форма, не утверждение о реальном автомобиле/артикуле):
```json
{"identifier":"WVWZZZ1KZAW000001","requested_part":"передние колодки 1 комплект"}
```
Вход, отклоняемый схемой до исполнения инструмента:
```json
{"identifier":null,"requested_part":"передние колодки 1 комплект"}
```

Выход: Legacy flat: ok/identifier/requested_part/requested_part_profile/city/vehicle_profile/field_statuses/field_evidence/provenance/missing_fields/family_candidates.; lookup_scope/identity_confidence/live_capability/pipeline/manual_public_search_queries/blockers/provider_status/privacy..
Ошибки и неполнота: Missing/disputed identity and unavailable configured providers remain blockers with lookup scope; a plan is separate from a successful provider result..

- Reads local source routing and provider configuration; does not decode an identifier or send provider requests.
- The optional vehicle_identity supplies planning context; no automatic VIN fitment, purchase, CRM write or provider authentication claim.

Подробный контракт: [справочник](../references/partsapi.md). Карточка и inputSchema согласованы versioned export; ручной цвет не является результатом проверки.
