# manager.normalize_parts_request — Сохранить все позиции запроса

Основной модуль: [E3](../modules/E3.md); другие модули: нет.
Классификация: active. Состояние реализации: implemented.
Источник: manager; первичная база: AutoStop versioned rules or explicit supplied inputs. Исполнение: pure.

Вызов: native Manager MCP `normalize_parts_request`.

Входы: `text`, `items`.
Defaults: `{"items":null,"text":""}`.
Обязательные facade поля: нет; ограничения конкретной операции всё равно применяются.
Текущая inputSchema и проверка версии — [D1](../modules/D1.md).

Синтетический вход (форма, не утверждение о реальном автомобиле/артикуле):
```json
{"text":"передние колодки 1 комплект; задний левый амортизатор 1 шт; неизвестная прокладка 2 шт"}
```
Вход, отклоняемый схемой до исполнения инструмента:
```json
{"text":null}
```

Выход: Envelope: schema_version/tool_id/ok/outcome/data/evidence/missing_fields/conflicts/warnings/execution; paths ниже относятся к data.; items[]: item_id/raw/intent/quantity/unit/quantity_status/side/axle/position; raw_part_number/normalized_part_number/brand/ean/number_kind/missing_fields сохраняются по каждой позиции.
Ошибки и неполнота: invalid_input при неверной форме, >500 items, duplicate item_id или false/negative/malformed quantity; partial при неизвестной quantity/unit/intent; явная quantity:null остается unknown, 0 остается0.

- Маркировка/EAN/наименование сами не становятся OEM.
- Pure; маркировка/EAN/наименование не становятся OEM. Сохраняется supplied item_id; иначе deterministic ID зависит от текста и позиции.

Подробный контракт: [справочник](../references/partsapi.md).
