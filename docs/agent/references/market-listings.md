# Объявления Avito и Baza.Drom

Сценарии: [E10 — Avito](../modules/E10.md), [E11 — Drom](../modules/E11.md). Независимые on-demand adapters читают внешние источники; они не связываются с продавцом и не меняют CRM/Store/marketplace.

| Источник | Private настройки | Инструменты и contract |
| --- | --- | --- |
| Avito / ReefAPI | `REEFAPI_API_KEY` | `avito_search_listings`, `avito_read_listing`, `assess_avito_price_sample`; [API](https://reefapi.com/docs/avito), `/avito/v1/search`, `/avito/v1/listing`. |
| Baza.Drom / Webbee | `WEBBEE_API_TOKEN`, `WEBBEE_DROM_ROBOT_ALIAS`, `AUTOSTOP_DROM_LISTINGS_ENABLED` | `drom_start_parts_search`, `drom_get_parts_search`; [task API](https://app.webbee-ai.ru/api-docs/swagger.yml). |

`catalog_provider_status(stage="market_listing")` показывает имена недостающих настроек без значений. `configured`/`authorization_status=unverified` не доказывают успешный запрос; `live_callable_now` учитывает настройки и switch. Оценивай доступ по результату каждого вызова.

Drom switch по умолчанию выключен; без разрешённого API access инструменты возвращают `webbee_disabled` без vendor запроса. Включение отдельно: provider grants API → private token → bounded non-customer task в изолированной среде → `AUTOSTOP_DROM_LISTINGS_ENABLED=1` при порученном выпуске. Avito от switch Drom не зависит. Статус текущего аккаунта проверяется заново, исторический тариф не принимается за текущий факт.

## Поиск и результаты

Используй точный OEM/article и название детали. Сначала Красноярск, затем другие регионы при реалистичной доставке. Full VIN, личные контакты и секреты отклоняются. Каждый результат — `lead` с source URL и observed time; цена, наличие и fitment продавца требуют отдельного подтверждения. Отобранные сведения сохраняются только в разрешённом кейсе CRM/Store.

Общая модель listing: `source`, `listing_id`, `url`, `title`, `description`, `price_rub`, `price_text`, `price_qualifier`, `city`, `condition`, `seller`, `delivery`, `availability`, `published_at`, `observed_at`, `status`, `fitment_confirmed`, `availability_confirmed`. Seller содержит публичные `name/type/rating/reviews_count/reviews`; delivery — `available/label`; неизвестное остаётся null.

`assess_avito_price_sample` без provider вызова принимает до 60 нормализованных listing и точный part number. Принимаются только явный артикул в title/description, фиксированная положительная цена, известное состояние, город, URL и observed time. Duplicate ID/URL учитывается один раз; состояние/города разделяются. Медиана появляется с трёх distinct ads в одном сегменте и описывает лишь выборку Avito. Она не равна E9 медиане независимых доменов и не подтверждает цену/наличие/fitment. Search ranking/page coverage ограничивают выборку.

Webbee: create → start → poll → JSON results. Queued response не означает completion; читай `drom_get_parts_search` с возвращёнными task ID/run UID, соблюдая quota и избегая duplicate starts. `elementCountLimit`/`pageCountLimit` ограничивают запуск; фактическая pagination робота проверяется live sample. Новый robot alias/export требует fixture update и свежего sample.

Auth, quota, timeout, malformed output и empty result — разные исходы; они не доказывают отказ MCP. Установка source не активирует runtime: после порученного [выпуска](deployment.md) сверяй tools/list, сначала dry-run, затем bounded non-customer provider call и exact listing read. Tokens, vendor payload и customer query не попадают в Git/docs.
