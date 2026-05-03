# MANIFEST

## Корневые файлы

- `README.md` — что это за пакет и порядок загрузки.
- `PROJECT_BOOTSTRAP_FOR_CODEX.md` — стартовое техническое задание для Codex.
- `MANIFEST.md` — список файлов.

## docs

- `00_executive_summary_ru.md` — концепция проекта.
- `01_market_analysis_ru.md` — анализ рынка Красноярска и России.
- `02_data_sources_ru.md` — источники данных и матрица доверия.
- `03_part_identity_ru.md` — идентификация детали и нормализация.
- `04_search_workflows_ru.md` — сценарии поиска.
- `05_api_integration_plan_ru.md` — план API-интеграций.
- `06_scoring_and_decision_model_ru.md` — скоринг предложений.
- `07_reporting_templates_ru.md` — шаблоны отчетов.
- `08_krasnoyarsk_vendor_discovery_ru.md` — поиск и ведение локальной базы продавцов.
- `09_compliance_limits_ru.md` — ограничения и законная автоматизация.
- `10_implementation_roadmap_ru.md` — дорожная карта разработки.
- `11_data_quality_and_feedback_ru.md` — качество данных и обратная связь.
- `12_source_references_ru.md` — справочник источников.
- `13_api_connector_specifications_ru.md` — спецификации коннекторов API.
- `14_pricing_and_averaging_model_ru.md` — проценка, медианы, стоимость простоя.

## prompts

- `PROMPT_TO_PASTE_IN_CODEX.txt` — главный промпт для Codex.
- `00_role_parts_sourcing_agent.md` — роль агента.
- `01_part_request_intake.md` — прием заявки.
- `02_fitment_identification.md` — применяемость.
- `03_api_search_task.md` — постановка API-поиска.
- `04_marketplace_manual_search.md` — ручной поиск на Авито/Дром/FarPost.
- `05_offer_scoring.md` — оценка предложений.
- `06_seller_call.md` — звонок продавцу.
- `07_no_result_escalation.md` — если ничего не найдено.
- `08_codex_build_tasks.md` — задачи разработки.

## schemas

JSON Schema (схемы JSON) для заявки, предложения, продавца, отчета и обратной связи.

## configs

YAML/ENV-конфигурации источников, весов скоринга, поисковых шаблонов, категорий и брендов.

## data

CSV/JSONL-данные: источники, шаблоны запросов, синтетические заявки и офферы, словарь синонимов, риск-карта, чек-листы.

## code_skeleton

Python-скелет: модели, нормализация, скоринг, безопасные ручные запросы, отчет, пример запуска.

## openapi

Черновой OpenAPI-контракт внутреннего шлюза поиска запчастей.
