# manager.j1_research_start — Начать bounded исследование J1

Основной модуль: [E15](../modules/E15.md); другие модули: нет.
Классификация: active. Состояние реализации: implemented.
Источник: public_web; первичная база: primary URL lineage must be preserved. Исполнение: job_write.

Вызов: native Manager MCP `j1_research_start`.

Входы: `objective`, `queries`, `max_pages`, `automotive_context`, `profile`.
Defaults: `{"automotive_context":null,"max_pages":300,"profile":"general","queries":null}`.
Обязательные facade поля: `objective`.
Текущая inputSchema и проверка версии — [D1](../modules/D1.md).

Синтетический вход (форма, не утверждение о реальном автомобиле/артикуле):
```json
{"max_pages":3,"objective":"Открытые источники по замене тормозных колодок","queries":["manufacturer brake pad replacement documentation"]}
```
Вход, отклоняемый схемой до исполнения инструмента:
```json
{"objective":null}
```

Выход: Legacy flat schema=autostop.j1.research.v1: ok/job_id/status=queued/profile/max_pages/query_count/automotive_context..
Ошибки и неполнота: ok=false/error.code: objective_invalid_or_sensitive/queries_or_objective_invalid/queries_or_automotive_context_invalid/max_pages_invalid/j1_busy/j1_store_unavailable..

- Writes a durable local public-research job; a background worker later searches/fetches public pages.
- queued is not completed evidence. Supply nonempty queries or a valid automotive profile/context; starting a second active job is refused.

Подробный контракт: [справочник](../references/web-research.md).
