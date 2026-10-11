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

При подготовке независимого второго прохода подтверждено отдельное P2:
карточка `store_entity_context` ещё перечисляет `store_sourcing_offer` как exact
context, хотя реализация отклоняет его до HTTP. Вторая итерация исправляет
limitations/errors через source JSON и регенерацию; E9/F3 уже направляют в повторный
`store_search`. Эта запись первой итерации сохраняет открытый статус замечания.

Второй проход также подтвердил P2 в `collect_work_price_evidence`: невыбранный
`aggregate_evidence` снимал `missing_fields`, хотя возвращённый `provided_aggregate`
оставался null. Исправление selection/outcome и синтетический контрпример входят
во вторую итерацию; формат ответа и явный выбранный aggregate сохраняются.

## Итерации

| Цикл | Полный review | Обязательные проверки | Состояние |
| --- | --- | --- | --- |
| 1 | core/docs/code, автомобильные модули/каталоги/skills, все документы CRM | генерация, CommonMark links/anchors/lines, SDK/examples, effects, Manager release-gates, CRM ci и browser | Manager local PASS; CRM pin/ci и exact-SHA CI проверяются после публикации |
| 2 | повторная сверка всего реестра другими reviewer по коду/схемам | полный обязательный набор, точные producer/consumer pins | следующий проход |
| 3 | независимый итоговый полный review после последнего исправления | обязательный набор и exact-SHA CI, финальный каталог/инструкция выпуска | следующий проход |

Если найдено новое замечание, оно исправляется и весь реестр проверяется снова.
Статус успешной проверки относится к точной ревизии/дереву, не к произвольному
checkout или прежнему green CI.

Полные Manager gates первой итерации: 5 751 тест прошёл за 583,36 секунды,
без пропусков; coverage 87%, E2 branch coverage 87,87% (1 021/1 162).
Ruff, форматирование 234 файлов, mypy 96 source файлов, doctor и обе генераторные
проверки прошли. Сохраняется прежнее предупреждение fork-теста Python 3.12;
тест проходит. Предыдущий полный запуск выявил устаревший assert старой grouped
media help; проверка обеих настоящих command branches исправлена и весь набор
успешно повторён. Новый открытый S5/S6 относится к исправлениям второго цикла.

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
