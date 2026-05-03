# AI Parts Search Krasnoyarsk / Russia Knowledge Pack

Дата сборки: 2026-05-03

Назначение: дать агенту Codex структурированную методику поиска автозапчастей в наличии в Красноярске и под заказ по России.

Пакет не содержит коммерческие ключи API (Application Programming Interface — программный интерфейс приложения), не содержит кода обхода CAPTCHA (Completely Automated Public Turing test to tell Computers and Humans Apart — автоматический тест для отличия человека от компьютера) и не предполагает нарушение правил площадок.

Главная идея: агент должен строить проверяемую цепочку:

1. идентифицировать деталь по VIN (Vehicle Identification Number — идентификационный номер автомобиля), OEM (Original Equipment Manufacturer — производитель оригинального оборудования)-номеру, артикулу, бренду и применяемости;
2. получить кроссы и аналоги из каталогов;
3. проверить наличие через прямые B2B (Business-to-Business — бизнес для бизнеса) API поставщиков;
4. проверить Красноярск через локальные источники, 2ГИС, Яндекс Карты, телефоны, сайты магазинов, Авито и Дром;
5. оценить предложения по единой формуле: точность применяемости, фактическое наличие, срок, цена, гарантия, возврат, надежность продавца;
6. сформировать отчет: что найдено, что подтверждено звонком, что требует проверки, что рекомендовано купить.

## Быстрый порядок загрузки в Codex

1. `docs/00_executive_summary_ru.md`
2. `docs/01_market_analysis_ru.md`
3. `docs/02_data_sources_ru.md`
4. `docs/03_part_identity_ru.md`
5. `docs/04_search_workflows_ru.md`
6. `docs/05_api_integration_plan_ru.md`
7. `docs/06_scoring_and_decision_model_ru.md`
8. `docs/07_reporting_templates_ru.md`
9. `docs/08_krasnoyarsk_vendor_discovery_ru.md`
10. `docs/09_compliance_limits_ru.md`
11. `docs/10_implementation_roadmap_ru.md`
12. `docs/11_data_quality_and_feedback_ru.md`
13. `docs/12_source_references_ru.md`
14. `docs/13_api_connector_specifications_ru.md`
15. `docs/14_pricing_and_averaging_model_ru.md`
16. `prompts/PROMPT_TO_PASTE_IN_CODEX.txt`
12. затем `schemas/`, `configs/`, `data/`, `code_skeleton/`, `openapi/`.

## Что важно

- Авито и Дром должны рассматриваться как каналы ручного или разрешенного поиска, а не как источник для агрессивного парсинга.
- Основу автоматизации должны давать прямые API поставщиков, прайс-листы, официальные B2B-кабинеты, агрегаторы и проверка по телефону.
- Наличие на сайте не равно фактическому наличию. Для срочного ремонта нужна фиксация подтверждения: кто подтвердил, когда, где физически лежит деталь, можно ли резервировать.
- Для дорогих и VIN-зависимых деталей агент обязан явно показывать уровень уверенности и список причин риска.
