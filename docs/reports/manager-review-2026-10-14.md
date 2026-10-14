# Проверка Manager — 2026-10-14

R01: ядро Manager, native MCP и служебный SQLite ledger. Baseline:
`8861f8c4fd35bb8988f06e3d5e7c57eb04677047`. Две независимые исходные рецензии
подтвердили шесть групп ошибок; свежая независимая приёмка первого кандидата
обнаружила дополнительную R01-07. Проверка использовала синтетические adapters
и одноразовые SQLite, без внешних эффектов.

| Группа | Приоритет | Исправление |
| --- | --- | --- |
| R01-01 | P1 | Три labor/OEM web roots вынесены из event loop в bounded web pool; исходный collector budget включает очередь. Отмена удерживает реальный worker slot, queued abandoned работа не запускает adapter. |
| R01-02 | P2 | `max_queries` сохраняет исходный JSON integer type до validation; bool, float и строки отклоняются. |
| R01-03 | P2 | Failed verification/readback внутри массивов запрещает `completed`, индексы сохраняются в failure paths. |
| R01-04 | P2 | Общий raw-body guard применяется к transition verification до run/event writes и replay acceptance. |
| R01-05 | P2 | Run/events/steps readback и list с events используют одну SQLite read transaction. |
| R01-06 | P2 | Legacy check-and-ALTER migration сериализуется SQLite lock и откатывается при ошибке, сохраняя строки. |
| R01-07 | P2 | Успешный technical node прекращает наследование контекста к generic payload children; доменный failed state не означает failed readback. Прямые противоречия и явно названные вложенные checks сохраняют completion gate. |

До изменений новые регрессии дали 28 отказов и 10 положительных результатов.
После исправления исходные 38 проверки прошли. Для R01-07 новая array форма
ложного отказа была регрессией первого кандидата, dict форма существовала на baseline.
Дополнительные 44 controls дали на первом кандидате 24 отказа и 20 положительных
результатов; после исправления прошли все, вместе с прежними failed arrays и positive refs.
Обновлённый профиль MCP/executor, ledger, pricing и automotive contracts:
402 проверки прошли, включая 109 новых.
Отдельный frozen native SDK probe подтвердил отзывчивость обоих labor roots,
cancellation, сохранение baseline input fingerprints, encoded nested inputs,
правильные output families и отсутствие утечки FD/threads после drain.
После старта worker неизвестные HTTP/attempt counts возвращаются как `null`;
до старта они известны как ноль. Публичные input schemas, Python signatures,
пороги и SQLite schema сохранены. Отдельный generic ledger профиль прошёл 87 проверок;
mypy, Ruff, форматирование и проверки генерируемых каталогов прошли.

Обязательное завершение цикла: независимая приёмка замороженного diff,
полные [release gates](../../scripts/release-gates.sh), публикация проверенного
commit и CI именно этой ревизии. Фактические итоговые результаты и SHA
фиксируются после проверок в техническом журнале. Выпуск и проверка установленного
runtime выполняются по отдельному поручению; текущий результат — исходный candidate.

Следующему циклу остаются shared labor/research HTTP wall deadline и VIN batch
admission/deadline. Native caller timeout не останавливает уже начатое HTTP-чтение;
его слот остаётся занятым до реального завершения. Рабочие службы и конфигурация
в этом цикле не менялись.
