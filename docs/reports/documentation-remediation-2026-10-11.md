# Исправление замечаний документации AutoStop — 2026-10-11

Это датированный технический отчёт. Он не задаёт рабочий процесс; действующие правила —
[AGENTS](../../AGENTS.md), [A1](../agent/modules/A1.md), [M2](../agent/modules/M2.md) и
[выпуск](../agent/references/deployment.md).

## Исходная точка и границы

Исходный Manager main: `53ea64c2149d10039037475da00e251fa26bb9f1`; CRM main:
`30c6cb7c3e890e84593e34b9ac9fbf3df6961729`. Работа выполняется в отдельных чистых
worktrees. Исходный Manager `496ee3d…` содержит 18 незакоммиченных файлов;
их HEAD/status/байты сохраняются. Работающий Manager `bb1172845036…` и CRM
`d70909d7eb02…` не устанавливаются заново в рамках публикации исходников.

Набор проверки включает все 212 действующих материалов Manager, полный tracked
Markdown/JSON/config реестр, 122 карточки, пять проектных скиллов и 32 выбранных
внешних входа. История и черновики обозначены отдельно. CRM включает 24 исходных
Markdown и новый отчёт; связанный browser corpus — 233 записи инструкций.

Основа полного реестра — [датированная инвентаризация](documentation-review-2026-10-10.md).
С этим отчётом проверяется 217 instruction-материалов: 204 Markdown, 12 JSON и один
UI YAML. Дополнительно проверяются пять конфигурационных/CI-файлов, всего 222 записи.
CommonMark проверяет относительные пути от документа, якоря и исходные номера строк;
активных orphaned документов нет.

## Подтверждённые исправления

| Замечание | Исправление | Доказательство |
| --- | --- | --- |
| S1, P2: шесть ложных local_read | benchmark/digest — composed; фото/analytics/capabilities/runtime — network_read; источники Store и условия уточнены | [наблюдаемые эффекты](../../tests/test_catalog_effects.py), исходный реестр и генерируемые карточки |
| S2, P3: обещанный файл VIN-фото | Base64 JPEG-preview, content_type, metadata, разные source/preview SHA; Telegram file отдельно | mocked native фото-вызов без созданного файла; сохранён формат API |
| S3, P2: checkpoint-status создавал/мигрировал БД | отдельный SQLite mode=ro/query_only reader с согласованным WAL snapshot и совместимым успешным JSON | [CLI/read-only сценарии](../../tests/test_store_checkpoint_status.py); старые missing/legacy контрпримеры падали |
| S4, P3: M2 принимал numeric SHA | string/null и 40 lowercase hex; prevalidation до файловых операций | [M2 контрпримеры](../../tests/test_m2_journal_summary.py); четыре проверки падали до исправления |
| Внешний срез A4/A5 устарел | генерация по единственному установленному manifest Visualize 1.0.47 | portable и host checks разделены; версии не выбираются вручную |
| CRM, P3: orthogonal-only документация | README/API Guide различают generated orthogonal paths и сохранённые ручные Q/C-кривые | существующий browser canvas/API curve regression |
| Core, P3: media preview описывал --language | usage и справочник отделяют transcribe с language от preview без language | отрицательный CLI proof завершается media_language_invalid до file/runtime/systemd |

Документация допускает техническое обслуживание SQLite WAL/SHM при read-only
соединении; статус не выполняет SQL-записей, bootstrap, миграций или writable
fallback. Missing/unreadable, incomplete/legacy и corrupt/read-failure состояния
возвращают ограниченную ошибку и exit 1; полная схема без строки — успешный
`exists=false`. Рабочие digest/ack/CAS/reset пути сохраняют прежнее поведение.

В настоящем M2 прочитаны только технические признаки формата: 112 записей,
82 строковых SHA и 30 null; numeric SHA и pending отсутствуют. История не
переписывается; отдельный нормализатор старых SHA не добавляется.

Все 118 корректных native-примеров приняты настоящими SDK argument models,
а 118 отрицательных примеров отклонены; четыре planned-входа не вызываются.
Регистрация содержит 70 инструментов и сохраняет fingerprint input schemas
`acda0e26f25c1fc20842c08af29aad2bab26160fdbde758cd4bfe578ebf1d7a0`.
Постоянная [SDK-проверка](../../tests/test_automotive_catalog.py) проверяет модели
без invocation, сети или рабочей базы.

## Исправления второго полного прохода

Весь реестр повторно прочитан по опубликованному Manager
`0df106dae6779657f2c01bf200f1cdbf9dab468e`: 177 автомобильных материалов и
64 core/config/history/skill материала с пересечениями дают все 222 записи;
32 внешних входа проверены отдельно. Помимо схем использованы настоящие native
обработчики с синтетическими входами, mock transport и временными SQLite.

| Замечание | Исправление | Проверка |
| --- | --- | --- |
| S5, P2: offer объявлялся exact entity context | source JSON и карточка исключают `store_sourcing_offer`; данные предложения повторно читаются через search | отказ до HTTP; остальные семь exact context сохраняются |
| S6, P2: невыбранный aggregate скрывал неполноту | missing fields вычисляются по возвращённому `provided_aggregate` | два контрпримера и четыре контроля, включая явный пустой aggregate; формат API сохранён |
| S7, P3: старые номера E-модулей в пояснениях реализации | E2/E4/E6/E11/E15 согласованы с текущей картой; legacy CRM E8 явно отделён | имена API, constants и native schemas не меняются |
| S8, P2: повреждённые checkpoint поля роняли CLI | reader ограничивает Unicode/type/depth/serialization ошибки как state failure | 16 регрессий; strict JSON без Infinity/NaN и UTF-8 stdout, legacy fallback и корректный Unicode, без SQL-записей |
| S9, P2: G1 status/preview описывался как чистое чтение | composed classification, SQLite initialization/migration и native outputs/errors отражены в источнике | 16 native effect тестов и legacy migration proof; jobs/runtime/controller backfills отделены от business actions |
| S10, P3: восемь legacy карточек обещали общий helper envelope | outputs/errors описывают фактические flat/Store/owner/conductor ответы | 17 native сценариев без сети; completed handoff не означает успешную бизнес-запись |

Дополнительный независимый проход до публикации проверил оставшиеся крайние
случаи и ссылки на реализацию. S11 расширяет S8: реальные CLI-контрпримеры
`state_version/pending_page_limit=±Infinity` выдавали JSON, непринимаемый строгим
parser; reader теперь ограничивает и эту ошибку. S12 исправляет старую фразу
«preview не меняет registry» в Telegram-справочнике. S13 убирает безусловное
`not deployed` из статического source manifest: наличие runtime подтверждается
отдельным installed readback. S10 также включает исторический explain-category
helper; отсутствие fixture сохраняет `ok=true/missing=true/count=0`.

S14 исправляет soft metadata native `manager_automations`: описание явно
перечисляет initialization/migration, `readOnlyHint` и `idempotentHint` становятся
false. Настоящий status/readiness повторно меняет `upgraded_at`; новые hints
консервативно отражают эффект. `destructiveHint/openWorldHint` остаются false.
Probe проверяет девять effectful инструментов и отвергает старые G1 hints;
эта проверка предназначена для новой установленной ревизии. Входные/выходные
форматы, controller peer UID/roles, job intent/revision и runs/outbox не меняются.

S15 уточняет происхождение данных четырёх outside business инструментов:
CRM parts_store cards читаются через CRM Gateway, Store management/owner API
и conductor используют Store endpoints и отдельные Manager contracts/ledger.
`provider_id` у трёх Store entries указывает Store; у CRM router остаётся Manager.
Классификация business_write, входы, ответы и guarded полномочия не меняются.

S16 устраняет ограничение «только typed TecDoc» в двух OEM-карточках.
Контекст modification также допускает валидный родной Elcats/Japancats/SsangYong
reference; `official_epc` подтверждается отдельным доверенным первичным
источником. Публичная каталожная строка сама не становится primary evidence.
Два native контрпримера прошли с отдельным manufacturer source, без HTTP.

S17 учитывает изменение окружения во время проверки: установленный Visualize
сменился с 1.0.47 на 1.0.49, прежний каталог пакета
исчез. A4/A5 повторно сгенерированы по текущему единственному manifest;
37 навыков, 212 проектных материалов и 32 внешних входа сохраняются.
Host-specific проверка выполняется отдельно от переносимой проверки CI.

S18 исправляет весь набор J1 lifecycle metadata. Три status/results/report
обработчика создают или мигрируют общий SQLite cache; восемь VIN entrypaths
выполняют prune/reconciliation до domain checks, включая выключенную VIN-запись.
Истёкший private corpus удаляется, queue stub и stopped-worker state меняются;
повтор потерянной job снова меняет updated_at. Whole-tool hints этих восьми
вызовов: readOnly=false, destructive=true, idempotent=false; openWorld зависит
от способности. Start general job сохраняет прежние hints: capacity prune
выполняет worker, а не этот native вызов. VIN receipt ограничен сроком хранения:
тот же start key после expiry может создать новую job. J1/E15, источник JSON,
карточки и runtime справочники согласованы с этими наблюдениями.

S19 уточняет whole-tool idempotentHint у manager_automation_control: false.
Повтор идентичного preview меняет schema upgraded_at; jobs intent/revision и
outbox сохраняются. Backend idempotency/CAS для guarded mutations не меняется.
Probe проверяет 14 effectful declarations, отдельно от input fingerprints.
Синтетические native SQLite/tmpfs и отрицательные probe-сценарии: 65 passed;
26 прежних контрпримеров падали до исправления. Все тесты изолируют оба J1 cache.

Все 122 карточки заново сгенерированы из source JSON. Набор входных схем,
stable IDs/defaults и права сохранены; изменения native hints — S14/S18/S19. S9/S14/S19 не меняют действия контроллера G1: текущий status
не отправляет сообщений и не ставит действия в очередь, но инициализирует БД;
legacy migration может дополнить jobs/runtime/controller. Template preview без
обращения к существующему заданию не открывает registry. Общий checkpoint writer
и обычные рабочие get/decode пути не меняются.

## Исправления третьего полного прохода

Независимый reviewer после S18/S19 повторно сопоставил весь реестр и все
70 native tools с их цепочками обработчиков. S20 уточняет одну строку effects:
`j1_research_start` инициализирует SQLite и ставит job в очередь; публичную сеть
затем читает worker. Синтетический настоящий старт дал queued/one query/zero
collected documents без search/prune; следующий worker выполнил один mock search
и завершился.

S21 согласует верхний абзац VIN-справочника с уже доказанным lifecycle контрактом:
выключенный флаг запрещает domain writes, но `vin_ephemeral_state_lost` или другая
lifecycle ошибка может возникнуть до `vin_research_disabled`. Чтение не получает
новые страницы, однако cleanup/reconciliation и SQLite initialization возможны.
Новых подтверждённых замечаний в остальном реестре не найдено.

[Комплект выпуска](../agent/references/deployment.md#комплект-для-выпуска-документации)
дополнен проверкой точной пары исходников, неизменных persistent bindings,
зависимостей native runtime, не менее 30 GiB, свежего owner graph и ограничений
отката после новых business writes. Это подготовка следующего выпуска;
установленные службы, исходные WIP и живая структура сохраняются.

## Итерации

| Цикл | Полный review | Обязательные проверки | Состояние |
| --- | --- | --- | --- |
| 1 | core/docs/code, автомобильные модули/каталоги/skills, все документы CRM | генерация, CommonMark links/anchors/lines, SDK/examples, effects, Manager release-gates, CRM ci и browser | Manager local/hosted PASS ([CI](https://github.com/UgaChavis/AutostopManager/actions/runs/38118760767)); CRM local/hosted PASS ([CI](https://github.com/UgaChavis/AutostopCRM-V1/actions/runs/38122827247)); оба точных SHA перечитаны |
| 2 | весь реестр Manager; S5–S19 исправлены, S20/S21 переданы третьему циклу; CRM S7/S9 и exact Manager pin | полный обязательный набор, точные producer/consumer pins | Manager [775fdc8](https://github.com/UgaChavis/AutostopManager/commit/775fdc8926202e8dd7cd87262d0e26b98d1dc380): 5818 local PASS, coverage 87%, [exact CI](https://github.com/UgaChavis/AutostopManager/actions/runs/38125740732); CRM публикует согласованный pin после своих gates |
| 3 | весь реестр заново, независимый итоговый review после S20/S21 и инструкции выпуска | обязательный набор и exact-SHA CI, финальный каталог/инструкция выпуска | Manager публикуется после повторных gates; согласованный CRM pin проходит полный ci и независимое перечитывание |

Если найдено новое замечание, оно исправляется и весь реестр проверяется снова.
Статус успешной проверки относится к точной ревизии/дереву, не к произвольному
checkout или прежнему green CI.

Полные Manager gates первой итерации: 5 751 тест прошёл за 583,36 секунды,
без пропусков; coverage 87%, E2 branch coverage 87,87% (1 021/1 162).
Ruff, форматирование 234 файлов, mypy 96 source файлов, doctor и обе генераторные
проверки прошли. Сохраняется прежнее предупреждение fork-теста Python 3.12;
тест проходит. Предыдущий полный запуск выявил устаревший assert старой grouped
media help; проверка обеих настоящих command branches исправлена и весь набор
успешно повторён. S5/S6 закрыты во втором цикле вместе с S7–S16.

Первый CRM результат опубликован обычным push:
[`d70b187`](https://github.com/UgaChavis/AutostopCRM-V1/commit/d70b187cb72fca18c13c1835500938be66d34cc7).
Повторный полный `run_checks.ps1 -Profile ci` прошёл: 3 065 runtime тестов за
1 495,497 s, 36 backup/retention за 6,877 s, coverage ratchet 13/13;
global coverage 82,11%. Browser/core/temp probe и Stage1 прошли: change-feed
read p95 10,9 ms при лимите 50 ms, replay 0,7 ms при лимите 20 ms.
Ранний full run и первый isolated rerun не прошли feed threshold; их результаты
сохранены. Пороги и измеряемый код не менялись, exact baseline прошёл на том же
хосте. Повторный полный результат имеет direct exit 0 и неизменный source hash.
Единственный локальный skip — opt-in Docker packaging: он закрывается отдельным
обязательным hosted job для точного SHA
([CRM CI](https://github.com/UgaChavis/AutostopCRM-V1/actions/runs/38122827247)).

## Публикация и выпуск

Порядок цикла: Manager checks → commit/push в `AutostopManager` → exact-SHA CI →
immutable Git archive export → CRM pin/templates/checks → commit/push в
`autostopcrm-v1` → exact-SHA CI → независимое перечитывание опубликованной пары.
Обычный push; при upstream изменениях изменения согласуются и проверки повторяются.
Отчёт каждого цикла публикуется вместе с его результатом; пустые коммиты не нужны.

Комплект выпуска содержит последние опубликованные Manager/CRM SHA, content
hash, sealed producer parity, свежий технический graph и instruction-only preview,
подготовленный из этой пары. Preview не применяется при подготовке. Future apply
требует свежих graph/version, owner/CAS/idempotency и независимого readback;
сохраняет IDs, canvas, manual paths, связи и tool_statuses. JSON hashes проверяются
по исходным типам чисел, без JS-нормализации float в integer.

Будущий deploy использует отдельный чистый Manager checkout на `AutostopManager`
через поддерживаемый `AUTOSTOP_MANAGER_SOURCE_DIR`. Перед выпуском проверяются
не менее 30 GiB и место для build/rollback, installed tuple, фактический skill
entrypoint и исходный hash адресного исправления. YAML owner skill корректен в
новом source; старые source/installed входы требуют отдельной проверки загрузчика.
После выпуска проверяются installed REVISION/pin/hash, host catalogs, exact
GitHub ссылки и права живой страницы. Откат выполняется по действующему runbook
с сохранением рабочих данных. Установленные расхождения не закрываются одним push.

## Приёмка

Весь актуальный реестр имеет результат review; генераторы воспроизводят
публикуемые документы и каталог. Stable IDs, native inputSchema/defaults и права
сохранены. Синтетические сценарии не отправляют сообщения, не расходуют реальные
provider квоты и не меняют бизнес-записи или commissioning marks. Внешняя
доступность и живые коммерческие операции не выводятся из schema/dry-run успеха.
Обязательная непроверенная часть остаётся открытой; итоговое закрытие требует
чистого независимого review после последнего исправления.
