# manager.get_store_analytics_report — get_store_analytics_report

Справочный/отдельный контур; не automotive workflow

Основной модуль: outside; другие модули: нет.
Классификация: outside. Состояние реализации: implemented.
Источник: store; первичная база: AutoStop Store aggregate analytics (Store analytics API). Исполнение: network_read.

Вызов: native Manager MCP `get_store_analytics_report`.

Входы: `query`, `period`, `date_from`, `date_to`, `top_limit`.
Defaults: `{"date_from":null,"date_to":null,"period":"auto","query":"","top_limit":10}`.
Обязательные facade поля: нет; ограничения конкретной операции всё равно применяются.
Текущая inputSchema и проверка версии — [D1](../modules/D1.md).

Синтетический вход (форма, не утверждение о реальном автомобиле/артикуле):
```json
{}
```
Вход, отклоняемый схемой до исполнения инструмента:
```json
{"query":null}
```

Выход: Обезличенный aggregate-only отчёт: period/previousPeriod, summary, funnel, topPages/topProducts/clicks, comparison и meta..
Ошибки и неполнота: invalid_input сохраняет ошибочный вход как отказ; empty/partial/unsupported не success; configuration/auth/quota/provider/parse ошибки различаются для сетевого источника.

- Выполняет HTTP POST для чтения агрегатов указанного периода; метод POST здесь не означает бизнес-запись.
- Raw events, VIN, контакты и пользовательские идентификаторы не возвращаются; отчёт требует отдельно порученного bounded периода.
- Назначение и полномочия определяет профильный контур; automotive smoke не выполняет записи.
- Наличие в общем native inventory не разрешает business action или чтение клиента.

Подробный контракт: [справочник](../references/store-api.md).
