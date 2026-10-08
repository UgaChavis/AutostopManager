# manager.recommend_automotive_sources — Маршруты исследования и покрытия источников

Основной модуль: [E15](../modules/E15.md); другие модули: [E14](../modules/E14.md).
Классификация: active. Состояние реализации: implemented.
Источник: manager; первичная база: AutoStop versioned rules or explicit supplied inputs. Исполнение: local_read.

Вызов: native Manager MCP `recommend_automotive_sources`.

Входы: `brand`, `data_type`, `include_licensed`, `limit`.
Defaults: `{"brand":null,"data_type":null,"include_licensed":true,"limit":10}`.
Обязательные facade поля: нет; ограничения конкретной операции всё равно применяются.
Текущая inputSchema и проверка версии — [D1](../modules/D1.md).

Синтетический вход (форма, не утверждение о реальном автомобиле/артикуле):
```json
{"brand":"Audi","data_type":"recalls","include_licensed":false}
```
Вход, отклоняемый схемой до исполнения инструмента:
```json
{"brand":[]}
```

Выход: Legacy flat: ok/brand/matched_brand_key/data_type/matched_data_type_key/include_licensed/sources/open_dataset_endpoints/warnings/rules.; sources preserve source_id/name/category/access/legal_ingestion_status/url/requires_license/citation and brand/data-type match..
Ошибки и неполнота: Unknown brand/data type or licensed-only routes after filtering produce warnings and an empty/fallback route list; this is not a provider authentication or network error..

- Reads versioned local source maps/registry and returns routes; does not fetch source content or test authentication.
- License-dependent sources remain marked requires_license; route priority is not evidence of vehicle applicability.

Подробный контракт: [справочник](../references/web-research.md).
