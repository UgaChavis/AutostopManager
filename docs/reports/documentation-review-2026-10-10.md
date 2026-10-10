# Восстановление документации — 2026-10-10

Это исторический отчёт проверки, а не новый процесс обслуживания клиента или поручение на выпуск.
Действующая навигация начинается с [AGENTS.md](../../AGENTS.md) и [A1](../agent/modules/A1.md).
Клиентское поведение остаётся только в [manage-autostop-client](../../.agents/skills/manage-autostop-client/SKILL.md),
выбранном через [B2](../agent/modules/B2.md). Отчёт не включается в активный instruction graph.

## Исходный срез и охват

Исходный опубликованный Manager SHA: `bb1172845036aa28ad32b893e09152d61b853fba`.
Рабочий source: отдельный worktree `manager-docs-refresh/AutostopManager`.
Dirty checkout `/opt/AutostopManager` на `496ee3d98fc100acda8f1dd129e52be6ab3b36cd` сохранён;
его старые результаты не считаются состоянием свежей основной ветки.

На исходном SHA учтены все 215 Git-tracked материалов `AGENTS.md`, `docs/` и `.agents/skills/`:
202 Markdown, 12 JSON и один UI YAML. Из них 200 Markdown образуют действующую документацию:
47 модульных файлов (48 логических модулей вместе с A2=AGENTS), пять SKILL.md, AGENTS,
25 технических справочников и 122 генерируемые карточки. Два остальных Markdown — historical и draft.
Все 12 JSON проверены как данные, schemas или fixtures; UI YAML проверен по его назначению.
Датированный `elcats_coverage_report.md` остаётся связанным техническим evidence,
а `client-instruction-audit.md` — отдельной историей миграции.

Ручной семантический проход покрывает AGENTS, группы A/B/C/D/E/F/G/H/I/J/M,
пять проектных скиллов и связанные справочники. Полный инвентарь, Markdown navigation,
кодировки и JSON проверяются машинно. Карточки сверяются с registry и генератором:
104 active, 14 outside, один diagnostic и три historical описания операций.
Статус операции не исключает её действующую справочную карточку из проверки.
Все 43 PartsAPI метода остаются отдельными операциями одного facade; planned extensions
не получают executable invocation. MCP manifests содержат 70 native Manager и 24 CRM tools;
координатор также сверил эти declarations read-only в текущем сеансе.

## Итерация 1 — полный первичный проход

### Подтверждённые находки и исправления

- Два runtime-readable реестра не были достижимы из инструкций: `vin_oem_sources.json`
  и `automotive_sources/open_dataset_endpoints.json`. Добавлены профильные ссылки в
  [автомобильные источники](../agent/references/automotive-sources.md) и
  [контракт автомобиля](../agent/references/vehicle-identity.md); наличие маршрута не объявляется live-доступом.
- [Legacy category fixture](../agent/partsapi_category_index.json) получил ссылку из
  [PartsAPI](../agent/references/partsapi.md), сохранив `legacy_inactive` и запрет текущего API использования.
- В E15 и справочниках OEM/web/Elcats публичное чтение ещё называлось E8 после миграции E8 в жидкости.
  Названия исправлены на E15 и публичный CRM reader. Координатор также обновил MCP common instructions
  E11→E15 для search и описания search/fetch с общим CRM web gateway.
- [E9](../agent/modules/E9.md) уточняет lookup-only `store_sourcing_offer`: повторный search,
  без несуществующего entity context. Остальные Store contracts и полномочия сохранены.
- В FST SKILL.md путь к правилам доступа стал разрешённой кликабельной ссылкой.
  В `manage-owner-telegram` description с двоеточием был некорректным YAML; исправлены только кавычки.
  Остальные инструкции клиентского поведения и метаданные скиллов сохранены.
- [Manager runtime](../agent/references/manager-runtime.md), [выпуск](../agent/references/deployment.md)
  и [каталог](../agent/references/automotive-tools.md) различают переносимый
  `--check --project-only` для CI/source gates и полный `--check` для установленного хоста.
  Проверка каталога, навигации и orphaned активных файлов развивается в связанном коде отдельным исполнителем.
- Выпуск сверяет exact bundle, CRM pin/hash и сохранённые тексты, сохраняя ID/геометрию/связи/tool_statuses.
  A5 представлен ссылкой на полный указатель без замороженных counts/SHA; генераторы A4/A5/cards обновляет координатор.
- В свежем A4/A5 сохранилась внешняя ссылка Visualize 1.0.47 при установленной 1.0.49;
  её исправление выполняется регенерацией. Codex Security 0.1.32 в исходном свежем SHA уже актуален.

### Проверки документационного прохода

- Все 215 исходных tracked материалов классифицированы ниже; все 12 JSON декодируются.
- Обе JSON schemas проходят `Draft202012Validator.check_schema`; automotive registry и синтетический
  m2-record example проходят свои schemas. Настоящий journal append не выполнялся.
- `skill-creator/scripts/quick_validate.py` прошёл для всех пяти скиллов после исправления YAML.
- `audit_documentation(check_external_links=False)` после ручных правок: `ok=true`, warnings пусты,
  48 модулей, пять скиллов, 200 Markdown. `collect_instruction_inventory`: 212 проектных MD/JSON, issues пусты.
- Бюджет девяти основных инструкций на этом контрольном срезе: 31 656 из 32 768 байт.
- `git diff --check` прошёл. Полные release gates, генераторные проверки, host audit и GitHub CI
  фиксирует координатор после объединения изменений; этот подраздел не объявляет их завершёнными заранее.

### Общие проверки первой итерации

`scripts/release-gates.sh` завершился с `release_gates_ok=true`: 5 597 тестов прошли
за 853,71 секунды, общее покрытие 87%, E2 branch coverage 87,87% (1 021/1 162).
Ruff, форматирование 232 файлов и mypy для 96 source файлов прошли. Проверки automotive
registry, 122 генерируемых карточек, native MCP схем и переносимого каталога прошли.
Полный host `update-instruction-catalogs.py --check` также прошёл: 37 навыков,
244 входа (212 проектных документов и 32 внешних). Doctor не сообщил warnings.
В pytest на Python 3.12 осталось одно предупреждение существующего fork-теста Telegram
о многопоточном процессе; тест прошёл, runtime bridge этой задачей не изменялся.

CRM: 77 профильных API/docs тестов и 20 browser regression сценариев прошли.
Просмотр ссылок проверен на desktop 1440×900 и mobile 390×844; console/page errors пусты.
Browser plugin в сеансе отсутствует, поэтому использован проектный Playwright Chromium
в отдельном временном каталоге. Все данные синтетические, production записи отсутствуют.
Полный CRM CI и GitHub CI фиксируются после экспорта каталога из опубликованного Manager SHA.

Для второго полного прохода отмечены два конкретных места: абсолютные runtime-ссылки
в коротком CRM A5 pointer и старый regex Markdown checker CRM. Они остаются открытыми
до повторной проверки; первая итерация не является окончательной приёмкой.

### Непроверенное и границы результата

В этом проходе не выполнялись deployment, перезапуски, journal write, CRM/Store записи,
Telegram/Gmail/Instagram отправки или запуск внешних jobs. Live delivery, внешняя точность
VIN/OEM, свежая robots политика, лицензии, provider availability/quota и реальные расчёты
не подтверждаются структурной проверкой документации. Исторические наблюдения сохраняют дату и область.
Изменение GitHub не обновляет установленный snapshot, pinned CRM bundle или live-тексты конструктора:
в текущей задаче live-состояние проверяется только read-only. Артефакты и инструкции
готовятся для будущего выпуска, который требует отдельного разрешения на deployment и live-обновление.

## Итерация 2 — соответствие реализации

Повторный проход выполнен по опубликованному Manager
`bb1490d35c9bd258c77b44d236634d1d76da848e` в отдельном неизменяемом Git archive.
[GitHub CI этой ревизии](https://github.com/UgaChavis/AutostopManager/actions/runs/38034563738)
прошёл: 5 513 тестов, 84 skipped, покрытие 86%, E2 branch coverage 87,87%.
Все 5 597 тестов того же набора выполнены локально; переносимый CI и проверки
установленных системных возможностей учитываются отдельно. В следующие CI прогоны
добавлен `-ra`, чтобы причины пропусков были видны в опубликованном журнале.

### Полнота повторного review

- Все 216 материалов повторно загружены и прочитаны: 203 Markdown, 12 JSON и один UI YAML.
  Aggregate SHA-256 исходного полного набора: `6feec0ad3a463f0e167e0e4860d50712ec3da245bcf85a04dc353c7de89035d9`.
- Ручной семантический проход покрывает 76 активных ручных Markdown: AGENTS, 45 модулей,
  пять скиллов и 25 справочников. 124 генерируемых Markdown — 122 карточки и A4/A5 —
  сверены с источниками и генераторами. Три historical/draft Markdown прочитаны как история.
- Повторная проверка portable registry: 212/212 документов, пять скиллов, нет orphaned.
  Automotive registry: 15 модулей, 122 операции, 70 native tools, 43 PartsAPI метода;
  schemas и MCP fingerprint согласованы. Обе JSON schemas и синтетический M2 record прошли проверку.
- 17 сценариев authority/routing выполнены in-memory с запретом сети: owner/client selection,
  spoofed name, group/outgoing/stale events, выбор скилла, консультация/согласование/запись,
  количество позиций, неизвестные количество/цена/применимость, fail-closed write context
  и pure ActionContract. Provider calls, сообщения, business writes и journal append отсутствуют.

### Подтверждённые исправления

- `prepare_action_contract` описан по реализации: проверяет вход и строит `ActionContractV2`
  без guarded token, сети и записи. Guard/proof проверяет конкретный исполнитель.
- Дублирующиеся строки VIN-стенда объединены. Описаны действующие default timeout 25 секунд,
  диапазон `(0, 60]`, stdin-only VIN и отдельный внешний process deadline.
- Указание на «будущий» Store release/feature branch заменено условным порядком
  совместимости consumer перед producer без утверждения непроверенного Store live SHA.
- CommonMark parser исключает закрытый начальный YAML frontmatter из ссылок, заголовков,
  таблиц и разделов, сохраняя исходные номера строк. Ссылки reference-style проверяются
  только при реальном использовании; code и image alt не создают ложных переходов.
- Проверка `:line` и границ Markdown-раздела считает только CR/LF исходного файла;
  Unicode separators остаются содержимым строки. Регрессии проверяют допустимые строки,
  выход за EOF и отсутствие ложной дополнительной строки в конце файла.
- POSIX path containment ускорен с сохранением `pathlib` семантики, Windows fallback
  и защит от выходов за корень. Full-source audit fixtures остаются полными и независимыми:
  четыре теста под coverage ускорились с 27,49 до 13,23 секунды;
  median warm inventory audit — с 0,777 до 0,626 секунды.
- Фокусная группа: 263 теста прошли; последняя проверка path compatibility — 38 тестов.
  Ruff, форматирование и mypy для изменённого кода прошли. Полные gates запускаются отдельно.
- Короткий CRM A5 pointer и portable CommonMark checker CRM остаются задачами второй
  CRM ревизии; их готовность и проверки фиксируются после публикации согласованной пары.

### Общие проверки второй Manager итерации

`scripts/release-gates.sh` завершился с exit 0 и `release_gates_ok=true`:
5 651 тест прошёл за 591,03 секунды, покрытие 87%, E2 branch coverage
87,87% (1 021/1 162). Ruff, форматирование 232 файлов и mypy для 96 source
файлов прошли. Переносимый каталог: 212/212, пять скиллов, нет orphaned или mismatches.
Полный host check: 37 навыков, 244 входа (212 проектных и 32 внешних), mismatches пусты.
Остаётся то же существующее предупреждение fork-теста Python 3.12; тест прошёл.

Дополнительный независимый CRM readback первой подготовленной ревизии сравнил
137 записей каталога (15 модулей и 122 инструмента): 602 ссылки совпали с CommonMark.
В portable template и infrastructure blueprint совпали по 412 ссылок; геометрия
48 узлов и 37 связей сохранилась. В синтетических формах найдены ещё три случая
для второго CRM прохода: nested image alt, escaped `!` и HTML comment.
Они не проявляются в текущих исходных текстах, но закрепляются регрессиями перед
окончательной приёмкой. Unsafe URL, role или cache bypass этот review не подтвердил.

## Итерация 3 — независимый полный review и исправления

Два новых независимых исполнителя полностью перечитали опубликованный Manager
`da9ac13094c14b5e93b138ce6572870fa4074014`, затем заново проверили исправленный candidate.
[GitHub CI второй ревизии](https://github.com/UgaChavis/AutostopManager/actions/runs/38037049324)
прошёл: 5 567 тестов, 84 skipped, покрытие 86%, E2 branch coverage 87,87%.
Причины всех пропусков опубликованы через `-ra`: 79 проверок требуют root/ownership,
installer или duty host, пять — локальных OCR/PDF возможностей. Полный локальный набор
выполнил все эти проверки без пропусков.

### Последние подтверждённые ошибки

- Automotive table validator использовал `splitlines()`, хотя CommonMark source maps
  считают только CR/LF. Unicode separators в тексте перед таблицей могли создавать
  ложное `module_tool_table_not_visible`. Теперь применяется общий `document_source_lines`.
  Одиннадцать регрессий проверяют восемь таких separators и LF/CRLF/CR controls;
  восемь новых случаев воспроизводили ошибку до исправления.
- Генератор метаданных скиллов вручную разбирал строки YAML: неверно обрабатывал
  quoted scalars, folded/literal description и `---` внутри значения. Использован
  `yaml.safe_load` с закрытым начальным frontmatter и обязательными непустыми строками
  `name`/`description`. Добавлена явная зависимость `PyYAML>=6.0,<7`.
  Двадцать два случая проверяют корректный YAML и отказ на malformed, nonmapping,
  bool/numeric/list/null values и Python object tags. До исправления 20 случаев падали.

Генерируемые A4/A5 и все 122 карточки остаются побайтно воспроизводимыми;
внешние форматы API, inputSchemas и права доступа не изменены.

### Повторная проверка после последнего исправления

- Проверены 216/216 материалов: 203 Markdown, 12 JSON и один UI YAML.
  Все 1 483 видимые Markdown-ссылки проверены; ошибок нет. Ручной семантический
  охват — все 76 активных ручных Markdown; 124 генерируемых файла сверены с источниками.
  Три historical/draft материала проверены как история. Активный graph — ровно 212 файлов.
- Один независимый reviewer выполнил 450 профильных тестов и 27 собственных
  contract scenarios; второй — 408 синтетических тестов с запретом сети и journal writes
  и 21 маршрутный walkthrough. Оба заново проверили весь реестр после исправлений;
  новых подтверждённых ошибок нет.
- Повторно сверены 15 automotive modules, 122 operations, 43 PartsAPI methods,
  все 70 native inputSchemas и 24 CRM declarations. Native fingerprint сохранён:
  `acda0e26f25c1fc20842c08af29aad2bab26160fdbde758cd4bfe578ebf1d7a0`.
- Переносимый каталог: 212/212, пять скиллов, нет orphaned/mismatches.
  Независимая полная host генерация и повтор координатора: 37 скиллов,
  244 входа = 212 project + 32 external, выходы побайтно совпадают.
- Все 528 tracked source файлов сопоставлены с независимым candidate snapshot;
  отличия от второй ревизии — только пять согласованных файлов реализации и тестов.
  После этого receipt координатор добавляет данный исторический раздел.

Полный `scripts/release-gates.sh` третьей итерации завершился с exit 0
и `release_gates_ok=true`: 5 684 теста прошли за 554,01 секунды, пропусков нет,
покрытие 87%, E2 branch coverage 87,87% (1 021/1 162).
Ruff, форматирование 232 файлов, mypy для 96 source файлов, doctor,
automotive registry и обе проверки генераторов прошли.
Сохраняется одно ранее отмеченное предупреждение fork-теста Python 3.12;
тест прошёл. Exact опубликованный SHA этой итерации и его GitHub CI
проверяются после обычного push; они указываются в итоговом отчёте задачи.

### Связанная CRM и подготовка выпуска

Первая CRM итерация опубликована в фактической основной ветке `autostopcrm-v1`:
[3ca93a6bdb4dd83fbc6eb42777312d22f5ddd780](https://github.com/UgaChavis/AutostopCRM-V1/commit/3ca93a6bdb4dd83fbc6eb42777312d22f5ddd780).
[Все GitHub CI jobs](https://github.com/UgaChavis/AutostopCRM-V1/actions/runs/38038055175)
этого SHA прошли. Обязательный `run_checks.ps1 -Profile ci` завершился
с подтверждённым direct-wait кодом 0: 3 047 runtime тестов и 36 release smoke тестов,
покрытие 82,11%, documentation/catalog/code health/JS/browser/performance gates прошли.
Первый запуск того же неизменного source сообщил успешные шаги, но процессная оболочка
вернула 143; этот результат не принят. Повторный полный запуск проверен по прямому
коду завершения PowerShell, без изменения source или обхода проверок.

Во второй CRM candidate исправлены прежние открытые случаи: CommonMark audit заменяет
regex для Markdown, A5 использует относительные ссылки, renderer корректно учитывает
nested image alt, escaped `!` и HTML comments. Профильные browser tests прошли;
полный обязательный CRM CI второй итерации выполняется отдельно.
Каталог candidate привязан к exact Manager SHA второй итерации; окончательная третья
CRM ревизия должна получить экспорт из опубликованного SHA третьей Manager итерации,
пройти полный CI и независимый readback. Этот подраздел не объявляет их завершёнными заранее.

Коммиты первой и второй Manager итераций:
[bb1490d35c9bd258c77b44d236634d1d76da848e](https://github.com/UgaChavis/AutostopManager/commit/bb1490d35c9bd258c77b44d236634d1d76da848e),
[da9ac13094c14b5e93b138ce6572870fa4074014](https://github.com/UgaChavis/AutostopManager/commit/da9ac13094c14b5e93b138ce6572870fa4074014).
Третья итерация — коммит, содержащий этот раздел; результат GitHub-only.
Порядок последующего согласованного выпуска описан в
[deployment](../agent/references/deployment.md) и
[automotive-tools](../agent/references/automotive-tools.md).
Работающие службы, установленный Manager snapshot, сохранённая живая структура CRM
и исходные пользовательские working copies в этой задаче не обновляются.

## Полный исходный инвентарь

Статус `active` означает действующий материал документации, а не разрешение исполнить описанную операцию.
`fixture` остаётся проверяемым и достижимым справочным входом; синтетический пример не является business action.
Historical/draft материалы читаются как история и не могут заменить актуальный клиентский скилл.
Этот новый отчёт — ещё один historical материал в `docs/reports/`, поверх исходных 215 файлов.

| Файл | Класс | Назначение |
| --- | --- | --- |
| [.agents/skills/manage-autostop-client/SKILL.md](../../.agents/skills/manage-autostop-client/SKILL.md) | active skill | полный профильный SKILL.md |
| [.agents/skills/manage-autostop-store/SKILL.md](../../.agents/skills/manage-autostop-store/SKILL.md) | active skill | полный профильный SKILL.md |
| [.agents/skills/manage-fst-vpn/SKILL.md](../../.agents/skills/manage-fst-vpn/SKILL.md) | active skill | полный профильный SKILL.md |
| [.agents/skills/manage-fst-vpn/agents/openai.yaml](../../.agents/skills/manage-fst-vpn/agents/openai.yaml) | active auxiliary | UI metadata FST skill; не Markdown-инструкция |
| [.agents/skills/manage-owner-instagram/SKILL.md](../../.agents/skills/manage-owner-instagram/SKILL.md) | active skill | полный профильный SKILL.md |
| [.agents/skills/manage-owner-telegram/SKILL.md](../../.agents/skills/manage-owner-telegram/SKILL.md) | active skill | полный профильный SKILL.md |
| [AGENTS.md](../../AGENTS.md) | active entry | полномочия и вход навигации |
| [docs/agent/automotive_offline_sources.json](../../docs/agent/automotive_offline_sources.json) | active data | versioned данные, routing или MCP manifest |
| [docs/agent/automotive_sources/automotive_repair_sources_catalog.json](../../docs/agent/automotive_sources/automotive_repair_sources_catalog.json) | active data | versioned данные, routing или MCP manifest |
| [docs/agent/automotive_sources/open_dataset_endpoints.json](../../docs/agent/automotive_sources/open_dataset_endpoints.json) | active data | versioned данные, routing или MCP manifest |
| [docs/agent/automotive_tools.json](../../docs/agent/automotive_tools.json) | active data | versioned данные, routing или MCP manifest |
| [docs/agent/automotive_tools.schema.json](../../docs/agent/automotive_tools.schema.json) | active schema | JSON Schema действующего контракта |
| [docs/agent/crm_mcp_catalog.json](../../docs/agent/crm_mcp_catalog.json) | active data | versioned данные, routing или MCP manifest |
| [docs/agent/drafts/e1-modernization-implementation-plan.md](../../docs/agent/drafts/e1-modernization-implementation-plan.md) | draft | план прежнего внедрения; вне активного графа |
| [docs/agent/elcats_catalog_registry.json](../../docs/agent/elcats_catalog_registry.json) | active data | versioned данные, routing или MCP manifest |
| [docs/agent/manager_mcp_catalog.json](../../docs/agent/manager_mcp_catalog.json) | active data | versioned данные, routing или MCP manifest |
| [docs/agent/modules/A1.md](../../docs/agent/modules/A1.md) | active module | действующий модуль; A2 соответствует AGENTS.md |
| [docs/agent/modules/A3.md](../../docs/agent/modules/A3.md) | active module | действующий модуль; A2 соответствует AGENTS.md |
| [docs/agent/modules/A4.md](../../docs/agent/modules/A4.md) | active generated index | генератор каталогов; ручные правки не допускаются |
| [docs/agent/modules/A5.md](../../docs/agent/modules/A5.md) | active generated index | генератор каталогов; ручные правки не допускаются |
| [docs/agent/modules/B1.md](../../docs/agent/modules/B1.md) | active module | действующий модуль; A2 соответствует AGENTS.md |
| [docs/agent/modules/B2.md](../../docs/agent/modules/B2.md) | active module | действующий модуль; A2 соответствует AGENTS.md |
| [docs/agent/modules/B3.md](../../docs/agent/modules/B3.md) | active module | действующий модуль; A2 соответствует AGENTS.md |
| [docs/agent/modules/B4.md](../../docs/agent/modules/B4.md) | active module | действующий модуль; A2 соответствует AGENTS.md |
| [docs/agent/modules/C2.md](../../docs/agent/modules/C2.md) | active module | действующий модуль; A2 соответствует AGENTS.md |
| [docs/agent/modules/C3.md](../../docs/agent/modules/C3.md) | active module | действующий модуль; A2 соответствует AGENTS.md |
| [docs/agent/modules/C4.md](../../docs/agent/modules/C4.md) | active module | действующий модуль; A2 соответствует AGENTS.md |
| [docs/agent/modules/C5.md](../../docs/agent/modules/C5.md) | active module | действующий модуль; A2 соответствует AGENTS.md |
| [docs/agent/modules/C6.md](../../docs/agent/modules/C6.md) | active module | действующий модуль; A2 соответствует AGENTS.md |
| [docs/agent/modules/C7.md](../../docs/agent/modules/C7.md) | active module | действующий модуль; A2 соответствует AGENTS.md |
| [docs/agent/modules/D1.md](../../docs/agent/modules/D1.md) | active module | действующий модуль; A2 соответствует AGENTS.md |
| [docs/agent/modules/D2.md](../../docs/agent/modules/D2.md) | active module | действующий модуль; A2 соответствует AGENTS.md |
| [docs/agent/modules/D3.md](../../docs/agent/modules/D3.md) | active module | действующий модуль; A2 соответствует AGENTS.md |
| [docs/agent/modules/D4.md](../../docs/agent/modules/D4.md) | active module | действующий модуль; A2 соответствует AGENTS.md |
| [docs/agent/modules/D5.md](../../docs/agent/modules/D5.md) | active module | действующий модуль; A2 соответствует AGENTS.md |
| [docs/agent/modules/E1.md](../../docs/agent/modules/E1.md) | active module | действующий модуль; A2 соответствует AGENTS.md |
| [docs/agent/modules/E10.md](../../docs/agent/modules/E10.md) | active module | действующий модуль; A2 соответствует AGENTS.md |
| [docs/agent/modules/E11.md](../../docs/agent/modules/E11.md) | active module | действующий модуль; A2 соответствует AGENTS.md |
| [docs/agent/modules/E12.md](../../docs/agent/modules/E12.md) | active module | действующий модуль; A2 соответствует AGENTS.md |
| [docs/agent/modules/E13.md](../../docs/agent/modules/E13.md) | active module | действующий модуль; A2 соответствует AGENTS.md |
| [docs/agent/modules/E14.md](../../docs/agent/modules/E14.md) | active module | действующий модуль; A2 соответствует AGENTS.md |
| [docs/agent/modules/E15.md](../../docs/agent/modules/E15.md) | active module | действующий модуль; A2 соответствует AGENTS.md |
| [docs/agent/modules/E2.md](../../docs/agent/modules/E2.md) | active module | действующий модуль; A2 соответствует AGENTS.md |
| [docs/agent/modules/E3.md](../../docs/agent/modules/E3.md) | active module | действующий модуль; A2 соответствует AGENTS.md |
| [docs/agent/modules/E4.md](../../docs/agent/modules/E4.md) | active module | действующий модуль; A2 соответствует AGENTS.md |
| [docs/agent/modules/E5.md](../../docs/agent/modules/E5.md) | active module | действующий модуль; A2 соответствует AGENTS.md |
| [docs/agent/modules/E6.md](../../docs/agent/modules/E6.md) | active module | действующий модуль; A2 соответствует AGENTS.md |
| [docs/agent/modules/E7.md](../../docs/agent/modules/E7.md) | active module | действующий модуль; A2 соответствует AGENTS.md |
| [docs/agent/modules/E8.md](../../docs/agent/modules/E8.md) | active module | действующий модуль; A2 соответствует AGENTS.md |
| [docs/agent/modules/E9.md](../../docs/agent/modules/E9.md) | active module | действующий модуль; A2 соответствует AGENTS.md |
| [docs/agent/modules/F1.md](../../docs/agent/modules/F1.md) | active module | действующий модуль; A2 соответствует AGENTS.md |
| [docs/agent/modules/F2.md](../../docs/agent/modules/F2.md) | active module | действующий модуль; A2 соответствует AGENTS.md |
| [docs/agent/modules/F3.md](../../docs/agent/modules/F3.md) | active module | действующий модуль; A2 соответствует AGENTS.md |
| [docs/agent/modules/F4.md](../../docs/agent/modules/F4.md) | active module | действующий модуль; A2 соответствует AGENTS.md |
| [docs/agent/modules/F5.md](../../docs/agent/modules/F5.md) | active module | действующий модуль; A2 соответствует AGENTS.md |
| [docs/agent/modules/G1.md](../../docs/agent/modules/G1.md) | active module | действующий модуль; A2 соответствует AGENTS.md |
| [docs/agent/modules/H1.md](../../docs/agent/modules/H1.md) | active module | действующий модуль; A2 соответствует AGENTS.md |
| [docs/agent/modules/H2.md](../../docs/agent/modules/H2.md) | active module | действующий модуль; A2 соответствует AGENTS.md |
| [docs/agent/modules/I1.md](../../docs/agent/modules/I1.md) | active module | действующий модуль; A2 соответствует AGENTS.md |
| [docs/agent/modules/I2.md](../../docs/agent/modules/I2.md) | active module | действующий модуль; A2 соответствует AGENTS.md |
| [docs/agent/modules/J1.md](../../docs/agent/modules/J1.md) | active module | действующий модуль; A2 соответствует AGENTS.md |
| [docs/agent/modules/M1.md](../../docs/agent/modules/M1.md) | active module | действующий модуль; A2 соответствует AGENTS.md |
| [docs/agent/modules/M2.md](../../docs/agent/modules/M2.md) | active module | действующий модуль; A2 соответствует AGENTS.md |
| [docs/agent/partsapi_category_index.json](../../docs/agent/partsapi_category_index.json) | fixture | legacy fixture; только диагностические операции, не текущий API путь |
| [docs/agent/references/automotive-offline.md](../../docs/agent/references/automotive-offline.md) | active reference | технический контракт или связанный датированный evidence |
| [docs/agent/references/automotive-sources.md](../../docs/agent/references/automotive-sources.md) | active reference | технический контракт или связанный датированный evidence |
| [docs/agent/references/automotive-tools.md](../../docs/agent/references/automotive-tools.md) | active reference | технический контракт или связанный датированный evidence |
| [docs/agent/references/client-instruction-audit.md](../../docs/agent/references/client-instruction-audit.md) | historical | датированный отчёт миграции; вне активного графа |
| [docs/agent/references/complete-backup.md](../../docs/agent/references/complete-backup.md) | active reference | технический контракт или связанный датированный evidence |
| [docs/agent/references/crm-mail.md](../../docs/agent/references/crm-mail.md) | active reference | технический контракт или связанный датированный evidence |
| [docs/agent/references/deployment.md](../../docs/agent/references/deployment.md) | active reference | технический контракт или связанный датированный evidence |
| [docs/agent/references/disk-maintenance.md](../../docs/agent/references/disk-maintenance.md) | active reference | технический контракт или связанный датированный evidence |
| [docs/agent/references/elcats.md](../../docs/agent/references/elcats.md) | active reference | технический контракт или связанный датированный evidence |
| [docs/agent/references/elcats_coverage_report.md](../../docs/agent/references/elcats_coverage_report.md) | active reference | технический контракт или связанный датированный evidence |
| [docs/agent/references/host-operations.md](../../docs/agent/references/host-operations.md) | active reference | технический контракт или связанный датированный evidence |
| [docs/agent/references/instagram-runtime.md](../../docs/agent/references/instagram-runtime.md) | active reference | технический контракт или связанный датированный evidence |
| [docs/agent/references/j1-vin-research.md](../../docs/agent/references/j1-vin-research.md) | active reference | технический контракт или связанный датированный evidence |
| [docs/agent/references/m2-journal-record.example.json](../../docs/agent/references/m2-journal-record.example.json) | fixture | синтетический пример validate; не настоящий append |
| [docs/agent/references/m2-journal-record.schema.json](../../docs/agent/references/m2-journal-record.schema.json) | active schema | JSON Schema действующего контракта |
| [docs/agent/references/manager-runtime.md](../../docs/agent/references/manager-runtime.md) | active reference | технический контракт или связанный датированный evidence |
| [docs/agent/references/market-listings.md](../../docs/agent/references/market-listings.md) | active reference | технический контракт или связанный датированный evidence |
| [docs/agent/references/oem-web.md](../../docs/agent/references/oem-web.md) | active reference | технический контракт или связанный датированный evidence |
| [docs/agent/references/offline-catalogs.md](../../docs/agent/references/offline-catalogs.md) | active reference | технический контракт или связанный датированный evidence |
| [docs/agent/references/part-market.md](../../docs/agent/references/part-market.md) | active reference | технический контракт или связанный датированный evidence |
| [docs/agent/references/partsapi.md](../../docs/agent/references/partsapi.md) | active reference | технический контракт или связанный датированный evidence |
| [docs/agent/references/public-aftermarket-catalogs.md](../../docs/agent/references/public-aftermarket-catalogs.md) | active reference | технический контракт или связанный датированный evidence |
| [docs/agent/references/store-api.md](../../docs/agent/references/store-api.md) | active reference | технический контракт или связанный датированный evidence |
| [docs/agent/references/telegram-runtime.md](../../docs/agent/references/telegram-runtime.md) | active reference | технический контракт или связанный датированный evidence |
| [docs/agent/references/vehicle-identity.md](../../docs/agent/references/vehicle-identity.md) | active reference | технический контракт или связанный датированный evidence |
| [docs/agent/references/vin-retest.md](../../docs/agent/references/vin-retest.md) | active reference | технический контракт или связанный датированный evidence |
| [docs/agent/references/web-research.md](../../docs/agent/references/web-research.md) | active reference | технический контракт или связанный датированный evidence |
| [docs/agent/references/work-pricing.md](../../docs/agent/references/work-pricing.md) | active reference | технический контракт или связанный датированный evidence |
| [docs/agent/tools/aftermarket-denso.md](../../docs/agent/tools/aftermarket-denso.md) | active generated reference | операция: active; из единого registry |
| [docs/agent/tools/aftermarket-mann.md](../../docs/agent/tools/aftermarket-mann.md) | active generated reference | операция: active; из единого registry |
| [docs/agent/tools/elcats-list-diagrams.md](../../docs/agent/tools/elcats-list-diagrams.md) | active generated reference | операция: active; из единого registry |
| [docs/agent/tools/elcats-list-groups.md](../../docs/agent/tools/elcats-list-groups.md) | active generated reference | операция: active; из единого registry |
| [docs/agent/tools/elcats-list-parts.md](../../docs/agent/tools/elcats-list-parts.md) | active generated reference | операция: active; из единого registry |
| [docs/agent/tools/elcats-lookup-candidates.md](../../docs/agent/tools/elcats-lookup-candidates.md) | active generated reference | операция: active; из единого registry |
| [docs/agent/tools/elcats-resolve-vehicle.md](../../docs/agent/tools/elcats-resolve-vehicle.md) | active generated reference | операция: active; из единого registry |
| [docs/agent/tools/extension-dtc.md](../../docs/agent/tools/extension-dtc.md) | active generated reference | операция: active; из единого registry |
| [docs/agent/tools/extension-frame-registry.md](../../docs/agent/tools/extension-frame-registry.md) | active generated reference | операция: active; из единого registry |
| [docs/agent/tools/extension-oem-epc.md](../../docs/agent/tools/extension-oem-epc.md) | active generated reference | операция: active; из единого registry |
| [docs/agent/tools/extension-service-procedures.md](../../docs/agent/tools/extension-service-procedures.md) | active generated reference | операция: active; из единого registry |
| [docs/agent/tools/manager-assess-avito-price-sample.md](../../docs/agent/tools/manager-assess-avito-price-sample.md) | active generated reference | операция: active; из единого registry |
| [docs/agent/tools/manager-assess-part-fitment.md](../../docs/agent/tools/manager-assess-part-fitment.md) | active generated reference | операция: active; из единого registry |
| [docs/agent/tools/manager-assess-part-market.md](../../docs/agent/tools/manager-assess-part-market.md) | active generated reference | операция: active; из единого registry |
| [docs/agent/tools/manager-avito-read-listing.md](../../docs/agent/tools/manager-avito-read-listing.md) | active generated reference | операция: active; из единого registry |
| [docs/agent/tools/manager-avito-search-listings.md](../../docs/agent/tools/manager-avito-search-listings.md) | active generated reference | операция: active; из единого registry |
| [docs/agent/tools/manager-benchmark-vin-parts-lookup.md](../../docs/agent/tools/manager-benchmark-vin-parts-lookup.md) | active generated reference | операция: diagnostic; из единого registry |
| [docs/agent/tools/manager-calculate-work-price.md](../../docs/agent/tools/manager-calculate-work-price.md) | active generated reference | операция: active; из единого registry |
| [docs/agent/tools/manager-capture-oem-evidence.md](../../docs/agent/tools/manager-capture-oem-evidence.md) | active generated reference | операция: active; из единого registry |
| [docs/agent/tools/manager-catalog-provider-status.md](../../docs/agent/tools/manager-catalog-provider-status.md) | active generated reference | операция: active; из единого registry |
| [docs/agent/tools/manager-collect-work-price-evidence.md](../../docs/agent/tools/manager-collect-work-price-evidence.md) | active generated reference | операция: active; из единого registry |
| [docs/agent/tools/manager-compare-part-relations.md](../../docs/agent/tools/manager-compare-part-relations.md) | active generated reference | операция: active; из единого registry |
| [docs/agent/tools/manager-compare-vehicle-modifications.md](../../docs/agent/tools/manager-compare-vehicle-modifications.md) | active generated reference | операция: active; из единого registry |
| [docs/agent/tools/manager-corgi-decode.md](../../docs/agent/tools/manager-corgi-decode.md) | active generated reference | операция: active; из единого registry |
| [docs/agent/tools/manager-decode-frame-local.md](../../docs/agent/tools/manager-decode-frame-local.md) | active generated reference | операция: active; из единого registry |
| [docs/agent/tools/manager-decode-vehicle-batch.md](../../docs/agent/tools/manager-decode-vehicle-batch.md) | active generated reference | операция: active; из единого registry |
| [docs/agent/tools/manager-decode-vehicle-identities.md](../../docs/agent/tools/manager-decode-vehicle-identities.md) | active generated reference | операция: active; из единого registry |
| [docs/agent/tools/manager-decode-vehicle-identity.md](../../docs/agent/tools/manager-decode-vehicle-identity.md) | active generated reference | операция: active; из единого registry |
| [docs/agent/tools/manager-decode-vin-vpic.md](../../docs/agent/tools/manager-decode-vin-vpic.md) | active generated reference | операция: active; из единого registry |
| [docs/agent/tools/manager-decode-wmi-local.md](../../docs/agent/tools/manager-decode-wmi-local.md) | active generated reference | операция: active; из единого registry |
| [docs/agent/tools/manager-decode-wmi-vpic.md](../../docs/agent/tools/manager-decode-wmi-vpic.md) | active generated reference | операция: active; из единого registry |
| [docs/agent/tools/manager-download-store-quote-vin-photo.md](../../docs/agent/tools/manager-download-store-quote-vin-photo.md) | active generated reference | операция: outside; из единого registry |
| [docs/agent/tools/manager-drom-get-parts-search.md](../../docs/agent/tools/manager-drom-get-parts-search.md) | active generated reference | операция: active; из единого registry |
| [docs/agent/tools/manager-drom-start-parts-search.md](../../docs/agent/tools/manager-drom-start-parts-search.md) | active generated reference | операция: active; из единого registry |
| [docs/agent/tools/manager-estimate-repair-work-cost.md](../../docs/agent/tools/manager-estimate-repair-work-cost.md) | active generated reference | операция: active; из единого registry |
| [docs/agent/tools/manager-exist-price-lookup.md](../../docs/agent/tools/manager-exist-price-lookup.md) | active generated reference | операция: active; из единого registry |
| [docs/agent/tools/manager-explain-partsapi-category-for-intent.md](../../docs/agent/tools/manager-explain-partsapi-category-for-intent.md) | active generated reference | операция: historical; из единого registry |
| [docs/agent/tools/manager-fetch-page-browser.md](../../docs/agent/tools/manager-fetch-page-browser.md) | active generated reference | операция: active; из единого registry |
| [docs/agent/tools/manager-fetch-page-excerpt.md](../../docs/agent/tools/manager-fetch-page-excerpt.md) | active generated reference | операция: active; из единого registry |
| [docs/agent/tools/manager-get-store-analytics-report.md](../../docs/agent/tools/manager-get-store-analytics-report.md) | active generated reference | операция: outside; из единого registry |
| [docs/agent/tools/manager-inspect-vehicle-identifier.md](../../docs/agent/tools/manager-inspect-vehicle-identifier.md) | active generated reference | операция: active; из единого registry |
| [docs/agent/tools/manager-j1-research-add-queries.md](../../docs/agent/tools/manager-j1-research-add-queries.md) | active generated reference | операция: active; из единого registry |
| [docs/agent/tools/manager-j1-research-cancel.md](../../docs/agent/tools/manager-j1-research-cancel.md) | active generated reference | операция: active; из единого registry |
| [docs/agent/tools/manager-j1-research-document.md](../../docs/agent/tools/manager-j1-research-document.md) | active generated reference | операция: active; из единого registry |
| [docs/agent/tools/manager-j1-research-record-facts.md](../../docs/agent/tools/manager-j1-research-record-facts.md) | active generated reference | операция: active; из единого registry |
| [docs/agent/tools/manager-j1-research-report.md](../../docs/agent/tools/manager-j1-research-report.md) | active generated reference | операция: active; из единого registry |
| [docs/agent/tools/manager-j1-research-results.md](../../docs/agent/tools/manager-j1-research-results.md) | active generated reference | операция: active; из единого registry |
| [docs/agent/tools/manager-j1-research-start.md](../../docs/agent/tools/manager-j1-research-start.md) | active generated reference | операция: active; из единого registry |
| [docs/agent/tools/manager-j1-research-status.md](../../docs/agent/tools/manager-j1-research-status.md) | active generated reference | операция: active; из единого registry |
| [docs/agent/tools/manager-j1-research-vin.md](../../docs/agent/tools/manager-j1-research-vin.md) | active generated reference | операция: active; из единого registry |
| [docs/agent/tools/manager-lookup-original-parts.md](../../docs/agent/tools/manager-lookup-original-parts.md) | active generated reference | операция: active; из единого registry |
| [docs/agent/tools/manager-lookup-public-automotive-evidence.md](../../docs/agent/tools/manager-lookup-public-automotive-evidence.md) | active generated reference | операция: active; из единого registry |
| [docs/agent/tools/manager-manager-automation-control.md](../../docs/agent/tools/manager-manager-automation-control.md) | active generated reference | операция: outside; из единого registry |
| [docs/agent/tools/manager-manager-automations.md](../../docs/agent/tools/manager-manager-automations.md) | active generated reference | операция: outside; из единого registry |
| [docs/agent/tools/manager-normalize-labor-time.md](../../docs/agent/tools/manager-normalize-labor-time.md) | active generated reference | операция: active; из единого registry |
| [docs/agent/tools/manager-normalize-parts-request.md](../../docs/agent/tools/manager-normalize-parts-request.md) | active generated reference | операция: active; из единого registry |
| [docs/agent/tools/manager-parts-store-cards.md](../../docs/agent/tools/manager-parts-store-cards.md) | active generated reference | операция: outside; из единого registry |
| [docs/agent/tools/manager-partsapi-catalog-lookup.md](../../docs/agent/tools/manager-partsapi-catalog-lookup.md) | active generated reference | операция: active; из единого registry |
| [docs/agent/tools/manager-plan-oem-parts-providers.md](../../docs/agent/tools/manager-plan-oem-parts-providers.md) | active generated reference | операция: active; из единого registry |
| [docs/agent/tools/manager-prepare-action-contract.md](../../docs/agent/tools/manager-prepare-action-contract.md) | active generated reference | операция: outside; из единого registry |
| [docs/agent/tools/manager-recommend-automotive-sources.md](../../docs/agent/tools/manager-recommend-automotive-sources.md) | active generated reference | операция: active; из единого registry |
| [docs/agent/tools/manager-reconcile-vehicle-identity.md](../../docs/agent/tools/manager-reconcile-vehicle-identity.md) | active generated reference | операция: active; из единого registry |
| [docs/agent/tools/manager-resolve-catalog-group.md](../../docs/agent/tools/manager-resolve-catalog-group.md) | active generated reference | операция: active; из единого registry |
| [docs/agent/tools/manager-resolve-vin-oem-parts.md](../../docs/agent/tools/manager-resolve-vin-oem-parts.md) | active generated reference | операция: active; из единого registry |
| [docs/agent/tools/manager-search-offline-parts-catalogs.md](../../docs/agent/tools/manager-search-offline-parts-catalogs.md) | active generated reference | операция: active; из единого registry |
| [docs/agent/tools/manager-search-partsapi-category-index.md](../../docs/agent/tools/manager-search-partsapi-category-index.md) | active generated reference | операция: historical; из единого registry |
| [docs/agent/tools/manager-search-web-multi.md](../../docs/agent/tools/manager-search-web-multi.md) | active generated reference | операция: active; из единого registry |
| [docs/agent/tools/manager-store-digest.md](../../docs/agent/tools/manager-store-digest.md) | active generated reference | операция: outside; из единого registry |
| [docs/agent/tools/manager-store-entity-context.md](../../docs/agent/tools/manager-store-entity-context.md) | active generated reference | операция: active; из единого registry |
| [docs/agent/tools/manager-store-management-action.md](../../docs/agent/tools/manager-store-management-action.md) | active generated reference | операция: outside; из единого registry |
| [docs/agent/tools/manager-store-owner-api.md](../../docs/agent/tools/manager-store-owner-api.md) | active generated reference | операция: outside; из единого registry |
| [docs/agent/tools/manager-store-owner-capabilities.md](../../docs/agent/tools/manager-store-owner-capabilities.md) | active generated reference | операция: outside; из единого registry |
| [docs/agent/tools/manager-store-quote-conductor.md](../../docs/agent/tools/manager-store-quote-conductor.md) | active generated reference | операция: outside; из единого registry |
| [docs/agent/tools/manager-store-runtime-status.md](../../docs/agent/tools/manager-store-runtime-status.md) | active generated reference | операция: outside; из единого registry |
| [docs/agent/tools/manager-store-search.md](../../docs/agent/tools/manager-store-search.md) | active generated reference | операция: active; из единого registry |
| [docs/agent/tools/manager-validate-partsapi-category-index.md](../../docs/agent/tools/manager-validate-partsapi-category-index.md) | active generated reference | операция: historical; из единого registry |
| [docs/agent/tools/manager-verify-oem-candidates-web.md](../../docs/agent/tools/manager-verify-oem-candidates-web.md) | active generated reference | операция: active; из единого registry |
| [docs/agent/tools/manager-vin-brand-details.md](../../docs/agent/tools/manager-vin-brand-details.md) | active generated reference | операция: active; из единого registry |
| [docs/agent/tools/manager-vininfo-decode.md](../../docs/agent/tools/manager-vininfo-decode.md) | active generated reference | операция: active; из единого registry |
| [docs/agent/tools/partsapi--decode-e-a-n13.md](../../docs/agent/tools/partsapi--decode-e-a-n13.md) | active generated reference | операция: active; из единого registry |
| [docs/agent/tools/partsapi--find-e-a-n13.md](../../docs/agent/tools/partsapi--find-e-a-n13.md) | active generated reference | операция: active; из единого registry |
| [docs/agent/tools/partsapi--get-fill-volumes.md](../../docs/agent/tools/partsapi--get-fill-volumes.md) | active generated reference | операция: active; из единого registry |
| [docs/agent/tools/partsapi--get-norms-makes.md](../../docs/agent/tools/partsapi--get-norms-makes.md) | active generated reference | операция: active; из единого registry |
| [docs/agent/tools/partsapi--get-norms-models.md](../../docs/agent/tools/partsapi--get-norms-models.md) | active generated reference | операция: active; из единого registry |
| [docs/agent/tools/partsapi--get-norms-motors.md](../../docs/agent/tools/partsapi--get-norms-motors.md) | active generated reference | операция: active; из единого registry |
| [docs/agent/tools/partsapi--get-norms-times.md](../../docs/agent/tools/partsapi--get-norms-times.md) | active generated reference | операция: active; из единого registry |
| [docs/agent/tools/partsapi--part-suggest.md](../../docs/agent/tools/partsapi--part-suggest.md) | active generated reference | операция: active; из единого registry |
| [docs/agent/tools/partsapi--v-i-ndecode.md](../../docs/agent/tools/partsapi--v-i-ndecode.md) | active generated reference | операция: active; из единого registry |
| [docs/agent/tools/partsapi-car-impoundment.md](../../docs/agent/tools/partsapi-car-impoundment.md) | active generated reference | операция: outside; из единого registry |
| [docs/agent/tools/partsapi-decode-v-i-nus.md](../../docs/agent/tools/partsapi-decode-v-i-nus.md) | active generated reference | операция: active; из единого registry |
| [docs/agent/tools/partsapi-get-applicability.md](../../docs/agent/tools/partsapi-get-applicability.md) | active generated reference | операция: active; из единого registry |
| [docs/agent/tools/partsapi-get-applicability2.md](../../docs/agent/tools/partsapi-get-applicability2.md) | active generated reference | операция: active; из единого registry |
| [docs/agent/tools/partsapi-get-article-criteria.md](../../docs/agent/tools/partsapi-get-article-criteria.md) | active generated reference | операция: active; из единого registry |
| [docs/agent/tools/partsapi-get-article-crosses.md](../../docs/agent/tools/partsapi-get-article-crosses.md) | active generated reference | операция: active; из единого registry |
| [docs/agent/tools/partsapi-get-article-media.md](../../docs/agent/tools/partsapi-get-article-media.md) | active generated reference | операция: active; из единого registry |
| [docs/agent/tools/partsapi-get-article.md](../../docs/agent/tools/partsapi-get-article.md) | active generated reference | операция: active; из единого registry |
| [docs/agent/tools/partsapi-get-articles.md](../../docs/agent/tools/partsapi-get-articles.md) | active generated reference | операция: active; из единого registry |
| [docs/agent/tools/partsapi-get-cars-by-oil-specification.md](../../docs/agent/tools/partsapi-get-cars-by-oil-specification.md) | active generated reference | операция: active; из единого registry |
| [docs/agent/tools/partsapi-get-cars.md](../../docs/agent/tools/partsapi-get-cars.md) | active generated reference | операция: active; из единого registry |
| [docs/agent/tools/partsapi-get-crosses-title.md](../../docs/agent/tools/partsapi-get-crosses-title.md) | active generated reference | операция: active; из единого registry |
| [docs/agent/tools/partsapi-get-crosses-with-brand.md](../../docs/agent/tools/partsapi-get-crosses-with-brand.md) | active generated reference | операция: active; из единого registry |
| [docs/agent/tools/partsapi-get-crosses.md](../../docs/agent/tools/partsapi-get-crosses.md) | active generated reference | операция: active; из единого registry |
| [docs/agent/tools/partsapi-get-engine.md](../../docs/agent/tools/partsapi-get-engine.md) | active generated reference | операция: active; из единого registry |
| [docs/agent/tools/partsapi-get-full-info-c-v.md](../../docs/agent/tools/partsapi-get-full-info-c-v.md) | active generated reference | операция: active; из единого registry |
| [docs/agent/tools/partsapi-get-makes.md](../../docs/agent/tools/partsapi-get-makes.md) | active generated reference | операция: active; из единого registry |
| [docs/agent/tools/partsapi-get-models.md](../../docs/agent/tools/partsapi-get-models.md) | active generated reference | операция: active; из единого registry |
| [docs/agent/tools/partsapi-get-part-weight.md](../../docs/agent/tools/partsapi-get-part-weight.md) | active generated reference | операция: active; из единого registry |
| [docs/agent/tools/partsapi-get-partname-by-brand-number.md](../../docs/agent/tools/partsapi-get-partname-by-brand-number.md) | active generated reference | операция: active; из единого registry |
| [docs/agent/tools/partsapi-get-passenger-car-info.md](../../docs/agent/tools/partsapi-get-passenger-car-info.md) | active generated reference | операция: active; из единого registry |
| [docs/agent/tools/partsapi-get-product-groups-by-brand-number.md](../../docs/agent/tools/partsapi-get-product-groups-by-brand-number.md) | active generated reference | операция: active; из единого registry |
| [docs/agent/tools/partsapi-get-search-tree.md](../../docs/agent/tools/partsapi-get-search-tree.md) | active generated reference | операция: active; из единого registry |
| [docs/agent/tools/partsapi-gosnomer2vin.md](../../docs/agent/tools/partsapi-gosnomer2vin.md) | active generated reference | операция: active; из единого registry |
| [docs/agent/tools/partsapi-part-name-suggest.md](../../docs/agent/tools/partsapi-part-name-suggest.md) | active generated reference | операция: active; из единого registry |
| [docs/agent/tools/partsapi-rus-name-suggest.md](../../docs/agent/tools/partsapi-rus-name-suggest.md) | active generated reference | операция: outside; из единого registry |
| [docs/agent/tools/partsapi-search-articles.md](../../docs/agent/tools/partsapi-search-articles.md) | active generated reference | операция: active; из единого registry |
| [docs/agent/tools/partsapi-tecdoc-crosses.md](../../docs/agent/tools/partsapi-tecdoc-crosses.md) | active generated reference | операция: active; из единого registry |
| [docs/agent/tools/partsapi-to-dopusk.md](../../docs/agent/tools/partsapi-to-dopusk.md) | active generated reference | операция: active; из единого registry |
| [docs/agent/tools/partsapi-to-makes.md](../../docs/agent/tools/partsapi-to-makes.md) | active generated reference | операция: active; из единого registry |
| [docs/agent/tools/partsapi-to-models.md](../../docs/agent/tools/partsapi-to-models.md) | active generated reference | операция: active; из единого registry |
| [docs/agent/tools/partsapi-to-oils.md](../../docs/agent/tools/partsapi-to-oils.md) | active generated reference | операция: active; из единого registry |
| [docs/agent/tools/partsapi-to-parts.md](../../docs/agent/tools/partsapi-to-parts.md) | active generated reference | операция: active; из единого registry |
| [docs/agent/tools/partsapi-to-types.md](../../docs/agent/tools/partsapi-to-types.md) | active generated reference | операция: active; из единого registry |
| [docs/agent/vin_oem_sources.json](../../docs/agent/vin_oem_sources.json) | active data | versioned данные, routing или MCP manifest |
