# manager.collect_work_price_evidence — Отдельное получение ценовых свидетельств

Основной модуль: [E13](../modules/E13.md); другие модули: нет.
Классификация: active. Состояние реализации: implemented.
Источник: public_web; первичная база: primary URL lineage must be preserved. Исполнение: composed.

Вызов: native Manager MCP `collect_work_price_evidence`.

Входы: `work_items`, `vehicle_context`, `city`, `sources`, `aggregate_evidence`, `deadline_seconds`, `max_queries`.
Defaults: `{"aggregate_evidence":null,"city":"Красноярск","deadline_seconds":20,"max_queries":4,"sources":null,"vehicle_context":null}`.
Обязательные facade поля: `work_items`.
Текущая inputSchema и проверка версии — [D1](../modules/D1.md).

Синтетический вход (форма, не утверждение о реальном автомобиле/артикуле):
```json
{"aggregate_evidence":{"observations":[]},"sources":["provided_aggregate"],"work_items":["DEMO работа"]}
```
Вход, отклоняемый схемой до исполнения инструмента:
```json
{"aggregate_evidence":{"quotes":[]},"sources":["provided"],"work_items":null}
```

Выход: Envelope: schema_version/tool_id/ok/outcome/data/evidence/missing_fields/conflicts/warnings/execution; paths ниже относятся к data.; observations/labor/sources/vehicle_context/provided_aggregate/work_items/deadline_seconds; execution.network_calls/attempts отражает фактические HTTP reads; aggregate передается отдельно и не разворачивается в synthetic observations.
Ошибки и неполнота: invalid_input для private-looking input/context, unknown sources, work_items>20 или invalid budget; partial при отсутствующем evidence, warning provider_error/deadline_exceeded; отдельные успешные результаты сохраняются.

- Неизвестный расход не ноль; pure calculation не читает experience/CRM.
- sources public_web создаёт ограниченные HTTP reads; provided_aggregate использует только явно переданный aggregate. Default public_web; network_calls/attempts явны.
- sources: public_web(default) и provided_aggregate; aggregate_evidence обязателен для provided_aggregate, тогда network_calls=0 без public_web.
- context только make/model/year/engine/transmission/vehicle_class; VIN/контакты запрещены для публичного поиска. max_queries1..4, deadline_seconds [0.1,60], timeout отдельного HTTP<=4s.

Подробный контракт: [справочник](../references/work-pricing.md).
