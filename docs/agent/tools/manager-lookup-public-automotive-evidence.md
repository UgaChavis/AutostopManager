# manager.lookup_public_automotive_evidence — Публичные recalls/TSB и технические routes

Основной модуль: [E14](../modules/E14.md); другие модули: нет.
Классификация: active. Состояние реализации: implemented.
Источник: public_web; первичная база: primary URL lineage must be preserved. Исполнение: network_read.

Вызов: native Manager MCP `lookup_public_automotive_evidence`.

Входы: `vin`, `make`, `model`, `model_year`, `topics`, `system`, `include_tsb`, `limit`, `timeout`.
Defaults: `{"include_tsb":false,"limit":10,"make":null,"model":null,"model_year":null,"system":null,"timeout":12.0,"topics":null,"vin":null}`.
Обязательные facade поля: нет; ограничения конкретной операции всё равно применяются.
Текущая inputSchema и проверка версии — [D1](../modules/D1.md).

Синтетический вход (форма, не утверждение о реальном автомобиле/артикуле):
```json
{"make":"Audi","system":"engine","topics":["fluids"]}
```
Вход, отклоняемый схемой до исполнения инструмента:
```json
{"vin":[]}
```

Выход: Legacy flat: ok/input_context/evidence[]/warnings/missing_context/confidence/rules.; Evidence retains source-specific recall/TSB metadata or official fluid-reference routes; input_context VIN is redacted..
Ошибки и неполнота: Missing make/model/year is reported in missing_context and source evidence; upstream failure is retained in evidence[].ok/error and makes top-level ok=false..

- Default topics are recalls and fluids; recalls/TSB can read NHTSA, while fluids returns official reference routes without fetching the documents.
- A model/year recall is not an open VIN campaign; TSB metadata is not a diagnosis or service procedure.
- Official fluid approval routes do not supply VIN/unit-specific capacity, level temperature or repair procedure.

Подробный контракт: [справочник](../references/automotive-sources.md).
