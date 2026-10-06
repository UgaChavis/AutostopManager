# Объявления Avito и Baza.Drom

Сценарии: [E10 — Avito](../modules/E10.md), [E10 — Drom](../modules/E10.md). Независимые on-demand adapters читают внешние источники; они не связываются с продавцом и не меняют CRM/Store/marketplace.

| Источник | Private настройки | Инструменты и contract |
| --- | --- | --- |
| Avito / ReefAPI | `REEFAPI_API_KEY` | `avito_search_listings`, `avito_read_listing`, `assess_avito_price_sample`; [API](https://reefapi.com/docs/avito), `/avito/v1/search`, `/avito/v1/listing`. |
| Baza.Drom / Webbee | `WEBBEE_API_TOKEN`, `WEBBEE_DROM_ROBOT_ALIAS`, `AUTOSTOP_DROM_LISTINGS_ENABLED` | `drom_start_parts_search`, `drom_get_parts_search`; [task API](https://app.webbee-ai.ru/api-docs/swagger.yml). |

`catalog_provider_status(stage="market_listing")` показывает имена недостающих настроек без значений. `configured`/`authorization_status=unverified` не доказывают успешный запрос; `live_callable_now` учитывает настройки и switch. Оценивай доступ по результату каждого вызова.

Drom switch по умолчанию выключен; без разрешённого API access инструменты возвращают `webbee_disabled` без vendor запроса. Включение отдельно: provider grants API → private token → bounded non-customer task в изолированной среде → `AUTOSTOP_DROM_LISTINGS_ENABLED=1` при порученном выпуске. Avito от switch Drom не зависит. Статус текущего аккаунта проверяется заново, исторический тариф не принимается за текущий факт.

## Поиск и результаты

Используй точный OEM/article и название детали. Сначала Красноярск, затем другие регионы при реалистичной доставке. Full VIN, личные контакты и секреты отклоняются. Каждый результат — `lead` с source URL и observed time; цена, наличие и fitment продавца требуют отдельного подтверждения. Отобранные сведения сохраняются только в разрешённом кейсе CRM/Store.

Общая модель listing: `source`, `listing_id`, `url`, `title`, `description`, `price_rub`, `price_text`, `price_qualifier`, `city`, `condition`, `seller`, `delivery`, `availability`, `published_at`, `observed_at`, `status`, `fitment_confirmed`, `availability_confirmed`. Seller содержит публичные `name/type/rating/reviews_count/reviews`; delivery — `available/label`; неизвестное остаётся null.

### Контракт E10

Поиск выполняет один запрос страницы, без скрытого перехода на следующие страницы. `page` допускает 1–30, `limit` — 1–50 принятых уникальных объявлений после проверки и удаления дублей. Обрабатывается максимум 50 строк ответа. Технические счётчики `provider_count`, `scanned_count`, `rejected_count`, `duplicate_count`, `unscanned_count` объясняют охват полученного массива; это не общее число объявлений на Авито.

MCP принимает целые JSON numbers для страницы, limit и цены 0–2 000 000 000 рублей, boolean для `delivery_only` и `dry_run`. Строки и boolean вместо целого отклоняются до обращения к поставщику. Ошибка схемы имеет MCP `isError=true`; доменный отказ сохраняет совместимый результат `ok=false`, `source="avito"`, `error` при MCP `isError=false`.

Поиск и чтение выполняются вне основного цикла MCP: максимум два одновременно работающих адаптера и два дополнительных ожидающих вызова на процесс. Бюджет ожидания — 30 секунд, включая очередь. Переполнение возвращает `provider_busy` с `stage="admission"`, истечение бюджета — `provider_wait_timeout`. `adapter_started` и `adapter_may_continue` показывают, началось ли выполнение и может ли оно продолжаться после ответа. Отмена или таймаут ожидания не останавливают уже выполняющийся HTTP; слот остаётся занят до завершения работы. Просроченный ожидающий вызов не обращается к адаптеру, автоматического повтора нет. `dry_run` выполняет только проверку запроса и не занимает очередь поставщика.

Нормализация поддерживает текущие поля ReefAPI и прежние fixtures: краткое `description_snippet`, параметры `params`/`parameters`, `delivery_available`/`delivery_text` и прежнее `delivery`/`avito_delivery`. Явное отсутствие доставки (`false`) сохраняется. Относительное `published_or_raised_text` не превращается в точный `published_at`; время наблюдения Manager остаётся отдельным фактом.

Состояние из `params` имеет приоритет над прежними полями; явное пустое значение не заменяется старым состоянием. Boolean `delivery_available` имеет приоритет над прежней доставкой; при противоречии ему не переносится старый label. `description_source` различает `description`, неполное `description_snippet` и отсутствие текста (`null`). Текст проходит ограничение длины и удаление чувствительных сведений; происхождение не гарантирует его полноту.

URL разбирается как адрес объявления с собственным ID. Обычное название детали рядом с ID не считается VIN; реальные VIN, контакты, секреты, другой домен и несовпадение ID/URL отклоняются. Адаптер удаляет безопасные tracking-параметры и fragment; оценщик принимает уже нормализованный HTTPS URL без них.

Отказы поставщика содержат безопасные `provider_code`, `http_status`, `retryable` без полного vendor message. `quota_exceeded`, `listing_not_found`, `rate_limited`, `authentication_failed`, `source_blocked`, `provider_timeout`, `provider_parse_error`, `provider_disabled` различают причины. Неизвестная ошибка остаётся общей. `retryable` описывает возможность повторения; адаптер не повторяет запрос автоматически. [Коды ReefAPI](https://reefapi.com/docs#errors).

`assess_avito_price_sample` без provider вызова принимает до 60 нормализованных listing и точный part number. Принимаются только явный артикул в title/description, фиксированная положительная цена, известное состояние, город, URL и observed time. Duplicate ID/URL учитывается один раз; состояние/города разделяются. Медиана появляется с трёх distinct ads в одном сегменте и описывает лишь выборку Avito. Она не равна E11 медиане независимых доменов и не подтверждает цену/наличие/fitment. Search ranking/page coverage ограничивают выборку.

Некорректное или будущее время наблюдения, а также дата-время без часового пояса исключают одну строку с `observed_at_invalid`; UTC overflow также не прерывает оценку остальных строк. Поддерживаемый формат даты без времени сохраняется. Оценщик сохраняет правило последнего подходящего наблюдения и не устанавливает новый предел давности этой выборки.

Webbee: create → start → poll → JSON results. Queued response не означает completion; читай `drom_get_parts_search` с возвращёнными task ID/run UID, соблюдая quota и избегая duplicate starts. `elementCountLimit`/`pageCountLimit` ограничивают запуск; фактическая pagination робота проверяется live sample. Новый robot alias/export требует fixture update и свежего sample.

Auth, quota, timeout, malformed output и empty result — разные исходы; они не доказывают отказ MCP. Установка source не активирует runtime: после порученного [выпуска](deployment.md) сверяй tools/list, сначала dry-run, затем bounded non-customer provider call и exact listing read. Tokens, vendor payload и customer query не попадают в Git/docs.
