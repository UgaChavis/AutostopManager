# manager.drom_get_parts_search — Статус и результаты vendor task Drom

Основной модуль: [E10](../modules/E10.md); другие модули: нет.
Классификация: active. Состояние реализации: implemented.
Источник: webbee; первичная база: Drom listings via Webbee. Исполнение: job_read.

Вызов: native Manager MCP `drom_get_parts_search`.

Входы: `task_id`, `uid`.
Defaults: `{}`.
Обязательные facade поля: `task_id`, `uid`.
Текущая inputSchema и проверка версии — [D1](../modules/D1.md).

Синтетический вход (форма, не утверждение о реальном автомобиле/артикуле):
```json
{"task_id":1,"uid":"DEMO"}
```
Вход, отклоняемый схемой до исполнения инструмента:
```json
{"task_id":null,"uid":"DEMO"}
```

Выход: Legacy flat: ok/source/status/task_id/uid/listings/count/observed_at/progress/fitment_confirmed/availability_confirmed..
Ошибки и неполнота: Legacy ok=false/source=drom/status/error/task_id/uid/outcome_uncertain for invalid reference, disabled/unconfigured transport, unavailable job status or provider failure..

- Reads external Webbee job status/results; pending and completed remain distinct.
- Replace synthetic task_id/uid with the exact pair from drom_start_parts_search; listing availability and fitment need independent confirmation.

Подробный контракт: [справочник](../references/market-listings.md).
