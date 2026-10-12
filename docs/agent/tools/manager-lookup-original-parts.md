# manager.lookup_original_parts — Оригинальные номера из переданных evidence

Основной модуль: [E4](../modules/E4.md); другие модули: нет.
Классификация: active. Состояние реализации: implemented.
Источник: manager; первичная база: AutoStop versioned rules or explicit supplied inputs. Исполнение: composed.

Вызов: native Manager MCP `lookup_original_parts`.

Входы: `identifier`, `model_year`, `make_hint`, `part_name`, `part_group`, `side`, `position`, `old_part_number`, `captured_oem_number`, `captured_source`, `captured_supersedes`, `captured_note`, `vehicle_identity`, `live_vpic`, `identifier_type`.
Defaults: `{"captured_note":null,"captured_oem_number":null,"captured_source":null,"captured_supersedes":null,"identifier_type":"auto","live_vpic":true,"make_hint":null,"model_year":null,"old_part_number":null,"part_group":null,"part_name":null,"position":null,"side":null,"vehicle_identity":null}`.
Обязательные facade поля: `identifier`.
Текущая inputSchema и проверка версии — [D1](../modules/D1.md).

Синтетический вход (форма, не утверждение о реальном автомобиле/артикуле):
```json
{"identifier":"WVWZZZ1KZAW000001","live_vpic":false,"make_hint":"Volkswagen","part_name":"передние колодки"}
```
Вход, отклоняемый схемой до исполнения инструмента:
```json
{"identifier":null}
```

Выход: Legacy flat dossier: ok/identifier/source_registry_version/decoded_vehicle/steps/hints/warnings.; catalog_routes/request/catalog_vehicle/provider_adapters/oem_candidates/supersessions/missing_context/fitment_confidence/next_actions..
Ошибки и неполнота: Malformed identifiers/ready identity binding are rejected; a skipped or failed vPIC decode remains visible in warnings and does not supply missing vehicle facts..

- This plans catalogue routes and captures explicitly supplied candidates; a route or OE reference does not verify an original part for the VIN.
- Bound vehicle_identity is reused without decode; foreign, failed or conflicted identity is rejected before provider calls.
- live_vpic=true is the legacy default; live_vpic=false avoids that provider read.

Подробный контракт: [справочник](../references/partsapi.md).
