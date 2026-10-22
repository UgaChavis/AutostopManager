# Единый автомобильный каталог

Registry: [automotive_tools.json](../automotive_tools.json); [JSON Schema](../automotive_tools.schema.json).
Модули имеют устойчивый module_key, инструменты — tool_id. E-code отображает задачу.
Markdown является текстом правил; inputSchema авторитетна в code/MCP.
Карточки генерирует scripts/generate-automotive-instructions.py; --check проверяет отсутствие drift.
Ссылки и классификация карточек берутся из registry; модульные Markdown поддерживаются отдельно и проверяются по его названиям, reference и таблицам операций.
Все actual native names учтены, 43 PartsAPI API-methods сопоставлены exact operation;
outside/historical/diagnostic entries явно отделены от активных автомобильных операций.

Положительный синтетический пример показывает форму входа, а не доступность поставщика.
invalid_example обязан отвергаться facade inputSchema до исполнения; nullable defaults не являются отрицательным входом.
Проверка этих примеров использует JSON Schema без вызова tools/providers. Доменные отказы проверяют отдельные unit tests;
не запускай подряд примеры job_write/business_write или составных инструментов с acquisition defaults.

`execution_kind` описывает допустимые эффекты всех ветвей инструмента: `pure` вычисляет по переданным данным,
`local_read` читает локальные источники; обе категории исключают HTTP и сохранение состояния операции.
`network_read` допускает удалённое чтение, даже если default или тёплый кеш обходятся без сети.
`composed` объединяет этапы обработки/получения данных или включает сохранение технического состояния:
benchmark сочетает локальные правила с vPIC/явно включённым PartsAPI, Store digest читает HTTP и пишет checkpoint,
а G1 status/readiness и preview существующего job могут создавать, обновлять или восстанавливать техническую схему registry.
Миграция старой схемы добавляет/заполняет технические поля jobs/runtime/controller, включая schedule_timezone и applied_at.
upgraded_at фиксирует создание записи компонента или смену версии; repair текущей версии сохраняет эту метку.
Повторный вызов на полностью актуальном registry сохраняет его техническое состояние.
Status/readiness/preview не выполняют предлагаемое guarded действие, не меняют desired_state/revision заданий,
не ставят сообщения в очередь и не отправляют их; templates и preview создания из template не обращаются к registry.
Условия default/cache/live и записи перечислены в `limitations` каждой карточки.
`provider_id` и `primary_data_source` указывают происхождение данных; `outside`/`diagnostic` определяют место в workflow
и не скрывают эффекты. Эта metadata дополняет native MCP annotations и не выдаёт полномочия.
Консервативные native G1 readOnlyHint=false/idempotentHint=false сохраняются: инициализация, обновление
версии и repair могут писать SQLite. Это относится к whole-tool annotations, а не обещает запись при каждом чтении.
Native `manager_automation_control` также сохраняет idempotentHint=false; guarded mutation
keys/CAS/receipts, входы, выходные форматы и права controller сохраняются.
J1 status/results/report имеют `execution_kind=composed`: обычная SQLite может создаваться/мигрировать,
а VIN-пути могут удалять истёкшие private jobs, обновлять lost-state stubs и восстанавливать worker state.
Все восемь VIN-aware tools имеют readOnly=false/destructive=true/idempotent=false; scoped domain
deduplication/receipts остаются в силе, но не делают lifecycle housekeeping идемпотентным.

## Проверка и экспорт

В isolated source worktree с отдельной venv и disposable state:

```bash
.venv/bin/python scripts/generate-automotive-instructions.py --check
.venv/bin/python scripts/check-automotive-catalog.py
.venv/bin/python scripts/update-instruction-catalogs.py --check --project-only
```

На целевом хосте полный `scripts/update-instruction-catalogs.py --check` дополнительно
сверяет выбранные установленные навыки Codex; CI не требует их локального кеша.

После публикации Manager и подтверждённого exact target SHA:

```bash
.venv/bin/python scripts/export-automotive-tools.py --revision FULL_PUBLISHED_MANAGER_SHA --output /PRIVATE/catalog.json
```

FULL_PUBLISHED_MANAGER_SHA заменяется фактическим 40-hex SHA. Export читает git archive,
а не bytes dirty checkout. Signature registration выполняется на temporary SQLite без provider calls.
Bundle schema=autostop.automotive-tools.bundle.v1; source_revision отдельно от content_hash.
Hash — SHA256 canonical JSON excluding source_revision/content_hash (ensure_ascii=false,sorted keys,compact separators).
В bundle входят тексты/хеши, registry, native_schemas, origins и migration map.

CRM committed pin: src/minimal_kanban/web_app_assets/source/automotive_tool_catalog.json.
Добавить export после публикации Manager; объём проверок и выпуск определяются поручением и [deployment](deployment.md).
Открытие окна не обращается к providers и не читает Manager checkout.
Coordinated deploy сравнивает installed Manager REVISION и CRM pin/hash.
Порядок schema→Manager producer→CRM pinned consumer; Store API не меняется.

## Статусы и права

В viewmode клик/Enter/Space модуля открывает центральное окно с инструкцией и карточками.
В editmode сохраняются выбор/drag/resize/connect конструктора.
Manual states: not_commissioned/temporarily_unavailable/working; absence отображается красным без записи GET.
Shared tool_id имеет одну запись tool_statuses. server назначает actor/time; owner/CAS/idempotency обязательны.
set_tool_status меняет ровно одну запись, clear_tool_status возвращает прежнее отсутствие для технического smoke.
Ни один цвет не запускает provider и не заменяет result evidence.
Состояние хранит CRM manager_structure.json; смена цвета не включает источник.
Каталог и Markdown поставляются readonly bundle: окно не читает dirty checkout и не обращается к providers.
Source Git SHA и content hash раздельны; миграция E-кодов сохраняет tool_id и не переносит прежние indicators в ручные цвета.
Полный release backup сохраняет statuses; portable graph template их не переносит.
Graph replace/layout/upsert/reroute сохраняют map, новая catalog revision не стирает старые IDs.

## Согласование инструкций и выпуск

Исходные правила хранятся по прежним путям: AGENTS, модули, навыки, карточки и справочники.
A2 соответствует AGENTS.md, остальные узлы — модульным Markdown по ID.
Автомобильный bundle и сохранённые тексты конструктора обновляются из опубликованного источника.
Для длинного A5 сохраняй короткую ссылку на полный указатель без зафиксированных количества файлов и SHA.

CRM scripts/sync_manager_structure_instructions.py готовит тексты, шаблоны и preview
из технического снимка, Manager root и exact bundle; режим check выявляет расхождения.
Обновление инструкции сохраняет ID, название, parent, геометрию, связи и tool_statuses.
Существующий scripts/migrate_e1_structure.py предназначен только для старой карты;
при обновлении текста его не запускай: он перестраивает E-геометрию.
Preview не меняет live state; применение — через owner/CAS/idempotency API с независимым readback.
При конфликте409 перечитай полный граф и заново подготовь текстовые изменения.
Release/rollback — [deployment](deployment.md); current/previous coherent tuple и business data сохраняются.
После открытия live writers нельзя вернуть старую DB ради отката UI.

## Контракт данных

Общие поля результата, происхождение, границы применения и полномочия — [E1](../modules/E1.md).
input_binding — односторонний SHA(kind:normalized_identifier); в технические отчёты его не сохраняй.
Reuse проверяет binding/kind и relevant engine/KPP/date/market контекст до provider call.

Числовые catalog IDs проверяются до преобразования: неподходящий тип, нечисловой
Unicode и превышение предела integer parsing возвращают существующий отказ.
Нулевые category IDs и допустимые положительные decimal IDs сохраняются.
Неизвестные типы relation/document kind остаются непроверенными свидетельствами.

`field_statuses` может содержать строковый статус либо объект с `status`.
`disputed` блокирует vehicle lookup в обеих формах; readiness модификации требует
`supported` для model_year, engine, transmission и market. Неподдерживаемая форма
карты статусов не подтверждает readiness модификации. Эти признаки не дают
права записи в CRM и не подтверждают точную применимость детали.
Спорные поля исключаются из manual search queries в обеих формах статуса;
спорная модель также исключает её canonical model_family, сохраняя отдельные family candidates.
Supersession учитывает объявленное направление `from_to`/`to_from`; ветви сравниваются
по нормализованному номеру и бренду. Разные подтверждённые замены одного исходного
номера сохраняются как конфликт, а raw endpoints остаются в результате.
Пустой результат нормализации номера не устанавливает primary OEM/supersession
и не считается совпадением двух endpoints.

Локальная нехватка HTTP worker или соединений возвращает существующие
`provider_busy`/`provider_executor_unavailable` без автоматического повтора.
Такой отказ не ухудшает и не восстанавливает состояние provider circuit;
ошибки сети, ответа поставщика и их retry правила сохраняются.
