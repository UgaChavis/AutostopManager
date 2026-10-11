# manager.store_runtime_status — store_runtime_status

Справочный/отдельный контур; не automotive workflow

Основной модуль: outside; другие модули: нет.
Классификация: outside. Состояние реализации: implemented.
Источник: store; первичная база: Manager adapter configuration/circuit state; optional Store runtime health or bootstrap snapshot. Исполнение: network_read.

Вызов: native Manager MCP `store_runtime_status`.

Входы: `live`, `bootstrap_snapshot`.
Defaults: `{"bootstrap_snapshot":false,"live":false}`.
Обязательные facade поля: нет; ограничения конкретной операции всё равно применяются.
Текущая inputSchema и проверка версии — [D1](../modules/D1.md).

Синтетический вход (форма, не утверждение о реальном автомобиле/артикуле):
```json
{}
```
Вход, отклоняемый схемой до исполнения инструмента:
```json
{"live":null}
```

Выход: Redacted readiness/summary и circuit state; meta.live_check для runtime, отдельный bootstrap snapshot при его запросе..
Ошибки и неполнота: invalid_input сохраняет ошибочный вход как отказ; empty/partial/unsupported не success; configuration/auth/quota/provider/parse ошибки различаются для сетевого источника.

- По умолчанию live=false и bootstrap_snapshot=false: читает только локальную конфигурацию/готовность/circuit адаптера без HTTP.
- live=true читает Store /runtime-status; bootstrap_snapshot=true читает /bootstrap-snapshot и имеет приоритет над live.
- Runtime/bootstrap чтение не продвигает digest checkpoint; локальная готовность не доказывает доступность или бизнес-результат Store.
- Назначение и полномочия определяет профильный контур; automotive smoke не выполняет записи.
- Наличие в общем native inventory не разрешает business action или чтение клиента.

Подробный контракт: [справочник](../references/store-api.md).
