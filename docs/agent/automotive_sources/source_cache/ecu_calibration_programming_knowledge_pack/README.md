# ECU calibration and programming knowledge pack

Дата создания: 2026-05-03

Назначение: обучающие материалы по законной диагностике, калибровкам, форматам файлов, программированию ECU, BMW workflow и диагностике экологических систем без отключения EGR/DPF/SCR.

## Состав

- data/ - справочные таблицы CSV и JSONL.
- sources/ - публичный каталог стандартов и официальных источников.
- MANIFEST.md - компактный список активных файлов и границы использования.

Сгенерированные PDF-дубли удалены 2026-05-08: активная база знаний индексирует
playbook/data/sources, поэтому отдельные PDF-копии только утяжеляли проект.
Длинные Markdown-модули удалены 2026-05-29 после переноса активных правил в
`docs/agent/ecu_calibration_programming_playbook.md`.

## Ограничение

Комплект не содержит инструкций по отключению EGR, DPF, SCR, катализаторов, readiness, MIL, DTC или обходу защиты ECU. Такие операции не являются ремонтной процедурой для дорожного автомобиля. Вместо этого добавлены диагностические маршруты, легальные workflow программирования и методы валидации ремонта.

## Рекомендуемый порядок чтения

1. `docs/agent/ecu_calibration_programming_playbook.md`
2. `data/`
3. `sources/`

## Для загрузки в базу сценариев

Используйте data/repair_scenario_cards.jsonl, data/risk_register.jsonl, data/programming_precheck_matrix.csv и data/glossary_ecu_programming.jsonl.
