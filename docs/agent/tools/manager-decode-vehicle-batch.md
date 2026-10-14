# manager.decode_vehicle_batch — Список через один выбранный decoder

Основной модуль: [E2](../modules/E2.md); другие модули: нет.
Классификация: active. Состояние реализации: implemented.
Источник: manager; первичная база: AutoStop versioned rules or explicit supplied inputs. Исполнение: composed.

Вызов: native Manager MCP `decode_vehicle_batch`.

Входы: `items`, `decoder`, `deadline_seconds`.
Defaults: `{"deadline_seconds":30}`.
Обязательные facade поля: `items`, `decoder`.
Текущая inputSchema и проверка версии — [D1](../modules/D1.md).

Синтетический вход (форма, не утверждение о реальном автомобиле/артикуле):
```json
{"decoder":"decode_wmi_local","items":["WVW","ZZZ"]}
```
Вход, отклоняемый схемой до исполнения инструмента:
```json
{"decoder":"decode_wmi_local","items":null}
```

Выход: Envelope: schema_version/tool_id/ok/outcome/data/evidence/missing_fields/conflicts/warnings/execution; paths ниже относятся к data.; decoder/items[{item_index,result}]/deadline_seconds; порядок и число строк сохраняются; execution суммирует известные network_calls/attempts; при неизвестном счётчике начатого sync read network_calls=null и network_calls_lower_bound содержит известную сумму.
Ошибки и неполнота: invalid_input для decoder/items/deadline; отдельная плохая строка получает invalid_input/provider_error; deadline_exceeded сохраняется на строках; общий outcome partial при любой неполной строке.

- Модельный год отдельно от даты производства; WMI не определяет двигатель.
- decoder: decode_vin_vpic/decode_wmi_vpic/decode_wmi_local/decode_frame_local/vin_brand_details/vininfo_decode/corgi_decode. До500 строк, deadline_seconds в [0.1,120].
- Один явно выбранный decoder, без fallback. Network только для vPIC. Отмена останавливает следующие строки; уже выданный sync read ограничен transport timeout.
- Очередь native async-вызова расходует исходный монотонный deadline; просроченные/отменённые строки не запускаются позже. Входной минимум проверяется один раз; малый остаток даёт строковый deadline_exceeded, сохраняя прежние строки и counters. Начатый decoder держит настоящий worker до завершения, upstream cancellation не утверждается.
- Native async использует отдельный bounded pool: 2 workers + 2 ожидающих незавершённых admissions. Отмена не отменяет concurrent future и не освобождает слот до фактического завершения; abandoned queued callback не начинает decoder. Busy/executor unavailable до начала дают envelope без input echo/network/retry; ContextVar сохраняет исходный absolute deadline.

Подробный контракт: [справочник](../references/vehicle-identity.md).
