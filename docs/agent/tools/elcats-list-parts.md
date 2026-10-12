# elcats.list_parts — Elcats: номера и условия на схеме

Прочитать строки схемы: OEM-кандидаты, позиции, количества и ограничения.

Основной модуль: [E4](../modules/E4.md); другие модули: [E6](../modules/E6.md).
Классификация: active. Состояние реализации: implemented.
Источник: elcats_catalog; первичная база: Public HTML catalog; actual Elcats/Japancats/Exist Ssangyong provider and per-route access policy remain explicit; not an official API. Исполнение: network_read.

Вызов: native Manager MCP `elcats_catalog_query`.
operation: `list_parts`.

Входы: `operation`, `vehicle_identity`, `part_request_item`, `catalog_ref`, `page_budget`.
Defaults: `{"catalog_ref":null,"page_budget":12,"part_request_item":null}`.
Обязательные facade поля: `operation`, `vehicle_identity`.
Текущая inputSchema и проверка версии — [D1](../modules/D1.md).

Синтетический вход (форма, не утверждение о реальном автомобиле/артикуле):
```json
{"catalog_ref":{"entity_kind":"diagram","entry_id":"elcats_vw","id":"DEMO-DIAGRAM","namespace":"elcats_epc","parameters":{"Group":"DEMO-GROUP","Mdl":"DEMO","Sub":"DEMO-DIAGRAM"},"parent_ref":{"entity_kind":"group","entry_id":"elcats_vw","id":"DEMO-GROUP","namespace":"elcats_epc","parameters":{"Group":"DEMO-GROUP","Mdl":"DEMO"},"parent_ref":{"entity_kind":"modification","entry_id":"elcats_vw","id":"DEMO-MODEL","namespace":"elcats_epc","parameters":{"Mdl":"DEMO"},"path":"/vw/Groups.aspx","provider":"elcats_catalog","vehicle_context":{"engine":"DEMO-ENGINE","make":"Volkswagen","market":"Europe","model":"Golf","production_date":"2010-01","transmission":"DEMO-GEARBOX"}},"path":"/vw/SubGroups.aspx","provider":"elcats_catalog","vehicle_context":{"engine":"DEMO-ENGINE","make":"Volkswagen","market":"Europe","model":"Golf","production_date":"2010-01","transmission":"DEMO-GEARBOX"}},"path":"/vw/Parts.aspx","provider":"elcats_catalog","vehicle_context":{"engine":"DEMO-ENGINE","make":"Volkswagen","market":"Europe","model":"Golf","production_date":"2010-01","transmission":"DEMO-GEARBOX"}},"operation":"list_parts","page_budget":2,"vehicle_identity":{"scope":"family","vehicle_profile":{"engine":"DEMO-ENGINE","make":"Volkswagen","market":"Europe","model":"Golf","production_date":"2010-01","transmission":"DEMO-GEARBOX"}}}
```
Вход, отклоняемый схемой до исполнения инструмента:
```json
{"operation":"list_parts","vehicle_identity":[]}
```

Выход: Envelope: tool_id/outcome/data/evidence/missing_fields/conflicts/warnings/execution; actual provider/namespace and source URL remain explicit.; Modification/group/diagram/part candidates and linked catalog refs; observed dates/options/position/quantity/supersession remain distinct.; execution preserves actual page calls, deadline/budget, partial/truncation and reuse; catalog rows do not claim exact VIN fitment..
Ошибки и неполнота: invalid_input or conflicting/unbound reference before catalog reads; unknown profile fields and ambiguous modifications remain explicit.; feature_disabled/robots_disallowed/not_commissioned/auth/challenge/unsupported_structure/empty/parse/provider/transport remain distinct; denied access is not absence of parts..

- Default-disabled AUTOSTOP_ELCATS_ENABLED; local route matrix includes every published brand and its actual readiness. Enabling the flag does not override robots restrictions.
- Public HTML adapter, not an official API or universal full-VIN EPC. Japancats and Exist Ssangyong retain separate provider/namespace lineage.
- Synchronous selected operation: page_budget default 12, max 24, total deadline 45 seconds. No decoder, hidden supplier fallback or browser/proxy/login bypass.
- Already supplied profile/ref can be reused; model year is not production date, unknown options/engine/transmission/market never receive silent defaults.
- Synthetic example validates input and reference contracts only; do not execute documentation examples. Public catalog evidence remains an OEM candidate until the required fitment evidence is available.
- Robots, redirects and public number images count against the same page budget. Optional local dual-PSM OCR remains unverified evidence, never exact fitment.

Подробный контракт: [справочник](../references/elcats.md).
