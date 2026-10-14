# Независимые нормочасы и стоимость работ

[E12](../modules/E12.md) получает исходные AUTONORMS rows; [E13](../modules/E13.md) отделяет acquisition от calculation.
normalize_labor_time(rows, unit, source) сохраняет raw workName/workTime/unit, normalized hours,
operation scope/source, included_operations и overlaps_with. Неизвестная единица — unknown, не часы по умолчанию.
collect_work_price_evidence принимает выбранные kinds/sources и обезличенный профиль,
либо explicit aggregates. Публичный research ограничен deadline; internal experience не загружается неявно.
calculate_work_price(labor, policy, hourly_rate, observations, unknown_costs) является pure.
policy.version и basis обязательны; hourly_rate либо сопоставимые public observations являются явной основой.
policy.coefficient/rounding/currency сохраняются. Unknown expense не ноль и оставляет total неизвестным.
Included operations не считаются дважды; unresolved overlap исключается с missing adjustment.
Пример: 2 часа × ставка1000, policy hourly_rate/version=demo-v1 → known_subtotal2000.
Неизвестная диагностика остаётся unknown/excluded; такой расчёт не обещает полный клиентский итог.
estimate_repair_work_cost совместим: defaults auto_research=true/use_internal_experience=true.
auto_research=false сам по себе не запрещает legacy experience read; pure helper не делает его вообще.
Exact signatures/примеры/ошибки — карточки tools; inputSchema — текущая MCP registration.

Native MCP выполняет collector, legacy estimate и OEM web verification в ограниченном
веб-пуле вне общего event loop. У collector исходный `deadline_seconds` включает очередь;
после неё оставшийся acquisition budget не начинается заново. Его `max_queries`
принимает только JSON integer 1–4; encoded nested JSON сохраняется. При отказе collector
сохраняет automotive envelope, а legacy estimate — своё плоское семейство ответа.
Не начавшийся worker имеет известные нулевые reads; после старта при timeout counts
неизвестны (`null`). Подробности cancellation/admission — [Manager runtime](manager-runtime.md).
Timeout-параметр отдельного labor HTTP read ещё не гарантирует общий wall deadline
на заголовки и тело: начатый поток может продолжиться и удерживает admission до завершения.

Обычный публичный поиск цен сохраняет общий privacy guard: VIN, контакты и секреты
не отправляются провайдеру и не отражаются в списке поисковых запросов. Явно переданные
VIN/номер кузова удаляются из всех частей research query и обезличиваются в ответе;
это не разрешает общий поиск по VIN. Новый collector отклоняет такие приватные входы
до обращения к источнику. Корректные обезличенные запросы сохраняют прежнее поведение.
Границы отдельных полей автомобиля сохраняются при составлении query, чтобы безопасные
make/model/year/engine не превращались при склейке в кажущийся split VIN. Quoted keys
секретов проверяются общим checker, в том числе после percent decoding.

Legacy estimate сохраняет только сопоставимые наблюдения: query задаёт намерение поиска,
а название работы, цена, время и labor/parts scope должны относиться к одной операции
в тексте источника. Заголовок о масле не превращает цену свечей из snippet в цену замены
масла. Непривязанные результаты остаются discovery data и не становятся ценовым основанием.
Однозначный заголовок может связать одну сумму или норму времени из snippet, если
snippet содержит только подпись цены/времени, город и scope, без другой работы или
предмета. Признак включённых запчастей при таком связывании сохраняет запрет цены работ.

Повтор одного наблюдения не увеличивает размер выборки: identity сохраняет операцию,
сумму, источник/lineage, locator и город. Противоречивые labor-only/includes-parts признаки
объединяются консервативно после нормализации boolean aliases. Разные страницы, города,
суммы и работы остаются отдельными наблюдениями.

Отрицательные и непредставимые числовые данные не становятся положительными ценами
или часами; переполнение промежуточного расчёта не должно обрывать весь ответ.
Явный `too_high/too_low` cross-check не допускает высокого decision confidence,
хотя сами основания, диапазон и применимая формула остаются в отчёте. Непредставимая
производная ставка не снимает уже доказанный `too_high`: цена и часы сравниваются
с прежним порогом без переполняющего деления. Нулевая вычисленная ставка остаётся
известным нулём и проверяется по прежнему `too_low`, а не как отсутствие значения. Ошибка чтения
тела public HTTP source фиксируется как недоступность источника без нового retry policy.
