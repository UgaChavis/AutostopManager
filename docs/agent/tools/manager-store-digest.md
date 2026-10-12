# manager.store_digest — store_digest

Справочный/отдельный контур; не automotive workflow

Основной модуль: outside; другие модули: нет.
Классификация: outside. Состояние реализации: implemented.
Источник: store; первичная база: AutoStop Store change feed plus Manager cursor/ack checkpoint (Store agent API and local SQLite). Исполнение: composed.

Вызов: native Manager MCP `store_digest`.

Входы: `baseline`, `since`, `cursor`, `ack_token`, `limit`, `stream`.
Defaults: `{"ack_token":null,"baseline":false,"cursor":null,"limit":25,"since":null,"stream":"store_digest"}`.
Обязательные facade поля: нет; ограничения конкретной операции всё равно применяются.
Текущая inputSchema и проверка версии — [D1](../modules/D1.md).

Синтетический вход (форма, не утверждение о реальном автомобиле/артикуле):
```json
{}
```
Вход, отклоняемый схемой до исполнения инструмента:
```json
{"baseline":null}
```

Выход: Bounded page/summary/items/changes; meta сообщает stream, baseline, checkpoint/delivery/ack и advancement..
Ошибки и неполнота: invalid_input сохраняет ошибочный вход как отказ; empty/partial/unsupported не success; configuration/auth/quota/provider/parse ошибки различаются для сетевого источника.

- Читает Store по HTTP и пишет локальный Manager checkpoint: baseline, pending delivery, cursor/ack и диагностическое состояние отказа.
- Первый успешный вызов может создать baseline без явного baseline=true; отсутствие бизнес-записи не делает операцию read-only.
- Без порученного кейса не вызывай production digest и не продвигай checkpoint ради smoke; raw provider payload не сохраняется.
- Назначение и полномочия определяет профильный контур; automotive smoke не выполняет записи.
- Наличие в общем native inventory не разрешает business action или чтение клиента.

Подробный контракт: [справочник](../references/store-api.md).
