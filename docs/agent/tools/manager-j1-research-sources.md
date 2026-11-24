# manager.j1_research_sources — Поставить публичные источники в очередь J1

Создать задание J1 по найденным ссылкам или добавить ссылки в своё задание того же типа

Основной модуль: [J1](../modules/J1.md); другие модули: нет.
Классификация: active. Состояние реализации: implemented.
Источник: public_web; первичная база: public URLs selected by the current agent; primary URL lineage must be preserved. Исполнение: job_write.

Вызов: native Manager MCP `j1_research_sources`.

Входы: `source_urls`, `objective`, `job_id`, `idempotency_key`, `automotive_context`.
Defaults: `{"automotive_context":null,"idempotency_key":null,"job_id":null,"objective":null}`.
Обязательные facade поля: `source_urls`.
Текущая inputSchema и проверка версии — [D1](../modules/D1.md).

Синтетический вход (форма, не утверждение о реальном автомобиле/артикуле):
```json
{"idempotency_key":"PUBLIC_DEMO_0001","objective":"Проверить описание детали","source_urls":["https://example.com/manual.pdf"]}
```
Вход, отклоняемый схемой до исполнения инструмента:
```json
{"source_urls":null}
```

Выход: Поля задания J1 `ok`, `schema`, `job_id`, `status`, `acquisition_mode`, `added`, `sources_total`, `max_pages`, `replayed`.
Ошибки и неполнота: Ошибки входа, бюджета, режима и состояния возвращаются в `error.code`. Код отказа исполнителя `j1_sources_executor_unavailable`.

- На старте передай objective и уникальный технический idempotency_key длиной 8–128 ASCII-символов. Первый символ — буква или цифра, далее допускаются также точка, дефис, двоеточие и подчёркивание. Для добавления передай job_id и массив source_urls.
- До 20 публичных HTTP(S) URL за вызов и до 300 разных URL на задание. VIN, контакты и секреты запрещены. Повторный URL определяется по canonical URL. Среди разных ссылок точная копия текста отмечается как дубликат, близкие версии доступны для сравнения.
- Вызов ставит URL в локальную очередь. Исполнитель J1 читает ссылки по действующим правилам доступа. Результаты и документы доступны через существующие вызовы.
- При известном job_id проверь статус и список принятых ссылок. Если ответ старта потерян вместе с job_id, точный повтор исходного тела с тем же ключом возвращает ID в пределах срока хранения задания. Удаление задания из кэша также удаляет привязку ключа.
- Обезличенный automotive_context передаётся только при создании. Добавленные ссылки наследуют контекст задания.

Подробный контракт: [справочник](../references/web-research.md).
