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

Оба цикла завершены и независимо приняты. По поручению владельца непрерывный
аудит заканчивается после текущего участка; дальнейшая работа — подготовка
серверного обновления Manager. Закрыты **20 подтверждённых групп**: 7 в R01 и 13
в R02. Исходные 20 WIP-файлов сохранены, их HEAD, status и SHA-256 сверены.

| Цикл | Опубликованный код | Полные локальные gates | CI точного SHA |
| --- | --- | --- | --- |
| R01 — core/MCP, 7 групп | [45db65a](https://github.com/UgaChavis/AutostopManager/commit/45db65a3b6f0cec42cbb420d0bc608dad03e4d2b) | 7117 PASS, 4 optional skips; coverage 87%, E2 branch 87,87% | [38543096455](https://github.com/UgaChavis/AutostopManager/actions/runs/38543096455): success, 6910 PASS, 211 skips, coverage 85% |
| R02 — VIN/OEM, 13 групп | [1cb6528](https://github.com/UgaChavis/AutostopManager/commit/1cb6528f004a96770c54525a911ce07ddcbcf87d) | 7313 PASS, 4 optional skips; coverage 87%, E2 branch 87,87% | [38561190145](https://github.com/UgaChavis/AutostopManager/actions/runs/38561190145): success, 7106 PASS, 211 skips, coverage 85% |

В обоих локальных прогонах осталось одно прежнее предупреждение Python 3.12
о fork. Дополнительные 207 CI-пропусков связаны с окружением и правами; причины
и количество совпадают между циклами. Все 196 новых R02 регрессий выполнены в CI.

R02 исправил deadline/admission выбранного decoder, изоляцию ошибочных строк и
сохранение результатов batch, типизированные факты и конфликты, vPIC/WMI и
происхождение copied provider facts. Broad факты и неизвестные range operators
не подтверждают конкретный fitment; префиксы, коллекции и boolean не становятся
OEM-номером. Прежний допустимый numeric scalar сохранён. Четыре nullable WMI поля
сохраняют форму ответа, а `null` остаётся отсутствием canonical факта. Подробности:
[отчёт VIN/OEM](autostop-manager-r02-vin-oem-source-review-20261014.md).

Каждый цикл включал две независимые исходные рецензии, отдельного автора и свежую
приёмку замороженного кандидата. Для R02 сохранены исходные 205 проверок:
192 PASS и 13 объяснённых срабатываний старых предпосылок; отдельно прошли
110 проверок уточнённых контрактов. Итоговое исправление WMI прошло независимые
111/111 случаев, включая неизменённые 41 прежний тест. Публичные input schemas
и Python signatures сохранены. Журнал M2, текущее состояние, индекс и сводка
независимо перечитаны; UUID результата R02 — `155392e6-c0f3-430d-83c3-71bf6c8b114d`.

Подготовка обновления закрепляет итоговый опубликованный SHA, отдельный чистый
clone на `AutostopManager`, sealed source с `REVISION`/manifest и экспорт каталога.
Проверяются зависимости существующего runtime, ёмкость и порядок backup/rollback.
Порядок выпуска: [deployment](../agent/references/deployment.md),
границы процессов и интерпретаторов: [Manager runtime](../agent/references/manager-runtime.md).
Заключительный документационный commit не меняет проверенный код; его точный SHA
также проверяется CI и закрепляется в приватном комплекте подготовки.

На момент подготовки установленный Manager — `8861f8c4fd35bb8988f06e3d5e7c57eb04677047`.
Все 564 manifest checksum верны; дополнительный `m2-journal.cpython-312.pyc`
препятствует строгому совпадению файлового дерева. Перед будущим выпуском нужны
свежие проверки и согласованный recovery set. Штатный orchestrator также требует
совместимый committed CRM pin и затрагивает CRM, scheduler и рабочий Telegram.
Свободного места было 29,5 GiB при документированном пороге 30 GiB плюс резерв;
подготовленный комплект не означает пройденный capacity gate или установленный релиз.

В очереди остаётся shared labor/research HTTP wall deadline; native caller timeout
не доказывает остановку уже начатого HTTP-чтения. Историческая неопределённость VIN
expiry сохранена. Новые участки, внешняя точность VIN/EPC и live business сценарии
не проверялись. Рабочие службы, конфигурация, CRM и Store в этой кампании не изменялись.
