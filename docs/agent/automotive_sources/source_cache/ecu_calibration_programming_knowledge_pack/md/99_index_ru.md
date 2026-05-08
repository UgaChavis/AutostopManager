# Индекс комплекта

# ECU calibration and programming knowledge pack

Дата создания: 2026-05-03

Назначение: обучающие материалы по законной диагностике, калибровкам, форматам файлов, программированию ECU, BMW workflow и диагностике экологических систем без отключения EGR/DPF/SCR.

## Состав

- md/ - Markdown-модули для машинной загрузки и ручного чтения.
- data/ - справочные таблицы CSV и JSONL.
- examples/ - синтетические учебные примеры A2L, ODX, HEX, S19, DCM, JSON.
- sources/ - публичный каталог стандартов и официальных источников.
- MANIFEST.md - компактный список активных файлов.

## Ограничение

Комплект не содержит инструкций по отключению EGR, DPF, SCR, катализаторов, readiness, MIL, DTC или обходу защиты ECU. Такие операции не являются ремонтной процедурой для дорожного автомобиля. Вместо этого добавлены диагностические маршруты, легальные workflow программирования и методы валидации ремонта.

## Рекомендуемый порядок чтения

1. 00_scope_and_boundaries_ru
2. 01_ecu_fundamentals_ru
3. 02_networks_uds_obd_ru
4. 03_file_formats_ru
5. 04_calibration_theory_ru
6. 05_oem_programming_workflow_ru
7. 06_bmw_programming_overview_ru
8. 07_emissions_diagnostics_ru
9. 08_flash_failures_recovery_ru
10. 09_validation_logging_ru
11. 10_service_scenarios_ru

## Для загрузки в базу сценариев

Используйте data/repair_scenario_cards.jsonl, data/risk_register.jsonl, data/programming_precheck_matrix.csv и data/glossary_ecu_programming.jsonl.


## Компактность

Сгенерированные PDF-дубли удалены 2026-05-08. Активные материалы сохранены в
Markdown/data/sources/examples.

## Файлы данных

- data/bmw_ecu_module_dictionary.csv
- data/file_format_index.csv
- data/generic_dtc_examples_emissions_network.csv
- data/glossary_ecu_programming.csv
- data/glossary_ecu_programming.jsonl
- data/learning_flashcards.jsonl
- data/programming_precheck_matrix.csv
- data/repair_scenario_cards.jsonl
- data/risk_register.jsonl
- data/uds_services_reference.csv

## Синтетические примеры

- examples/README_examples.md
- examples/toy_calibration_metadata.json
- examples/toy_dcm_values.dcm
- examples/toy_intel_hex.hex
- examples/toy_log_metadata_mdf.json
- examples/toy_motorola_srecord.s19
- examples/toy_odx_snippet.xml
- examples/toy_project_a2l.a2l
