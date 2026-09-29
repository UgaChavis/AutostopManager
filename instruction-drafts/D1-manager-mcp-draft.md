# D1 — AutoStopManager MCP

D1 — локальный сервер инструментов Manager. Codex выбирает нужную команду по задаче; D1 вызывает соответствующую функцию проекта или адаптер внешней системы и возвращает результат. Команды независимы: используй их в нужном порядке, а точные параметры проверяй в текущей схеме инструмента.

## АВТОМОБИЛЬ, ЗАПЧАСТИ И РАБОТЫ

- `decode_vehicle_identity`, `decode_vehicle_identities` — определить автомобиль по VIN или номеру кузова; второй инструмент обрабатывает список.
- `lookup_original_parts` — подготовить поиск оригинального номера и показать кандидатов и пробелы.
- `catalog_provider_status`, `plan_oem_parts_providers` — проверить доступность источников и построить план поиска.
- `partsapi_catalog_lookup` — запросить конкретную операцию каталога PartsAPI.
- `resolve_vin_oem_parts` — исследовать один запрос VIN и детали по доступному каталожному маршруту; результат остаётся кандидатом.
- `verify_oem_candidates_web` — сопоставить уже найденные номера с открытыми источниками.
- `public_aftermarket_catalog_lookup`, `exist_price_lookup` — проверить аналоги и публичные каталожные предложения.
- `search_offline_parts_catalogs` — поискать артикул в локальных каталогах.
- `search_partsapi_category_index`, `explain_partsapi_category_for_intent`, `validate_partsapi_category_index` — работать с историческим индексом категорий; он не заменяет текущий каталог PartsAPI.
- `recommend_automotive_sources`, `lookup_public_automotive_evidence` — подобрать источники и собрать публичные технические сведения.
- `benchmark_vin_parts_lookup` — проверить качество и пробелы маршрута подбора.
- `estimate_repair_work_cost` — оценить стоимость работ по доступным данным.
- `assess_part_market` — оценить предоставленные публичные предложения по детали.

Каталожный кандидат или рыночная цена сами по себе не подтверждают применимость, наличие и окончательную цену для клиента.

## ОБЪЯВЛЕНИЯ

- `avito_search_listings`, `avito_read_listing` — искать и читать объявления Авито через ReefAPI, если источник доступен.
- `drom_start_parts_search`, `drom_get_parts_search` — поиск объявлений Drom через Webbee; сейчас источник выключен, перед использованием проверь его статус.

## МАГАЗИН И СВЯЗАННЫЕ КАРТОЧКИ

- `store_runtime_status` — состояние подключения к Store.
- `store_digest`, `store_search`, `store_entity_context` — сводка, поиск и чтение конкретной записи Store.
- `store_owner_capabilities`, `store_owner_api` — доступные операции Owner API и их вызов.
- `store_management_action`, `store_quote_conductor` — управляемые изменения и работа с проценкой и предложением.
- `get_store_analytics_report` — агрегированный отчёт магазина.
- `download_store_quote_vin_photo` — получить фото VIN по конкретной проценке.
- `parts_store_cards` — работать с карточками CRM о запчастях через Manager.

## ПУБЛИЧНЫЙ ВЕБ И J1

- `search_web_multi`, `fetch_page_excerpt`, `fetch_page_browser` — поиск и чтение публичных страниц; браузер доступен при готовом окружении.
- `j1_research_start`, `j1_research_status` — запустить исследование и узнать его состояние.
- `j1_research_results`, `j1_research_document`, `j1_research_report` — читать результаты, документы и итоговый отчёт.
- `j1_research_add_queries`, `j1_research_cancel` — уточнить или остановить текущее исследование.

## АВТОМАТИЗАЦИИ И КОНТРАКТЫ

- `manager_automations` — посмотреть задания, шаблоны и готовность центра автоматизаций.
- `manager_automation_control` — управлять заданиями центра автоматизаций.
- `prepare_action_contract` — подготовить контракт для защищённого изменения в другой системе.

Перед действием проверь текущую цель и схему команды, а после изменения — результат в системе, которой принадлежат данные. Общего инструмента поиска инструкций и памяти D2 среди команд D1 сейчас нет; инструкции проекта Codex читает через терминал A3.
