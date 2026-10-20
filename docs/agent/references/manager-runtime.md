# AutoStop Manager: CLI, MCP и Codex

Модули: [A3 — CLI](../modules/A3.md), [D1 — MCP](../modules/D1.md), [D2 — знания](../modules/D2.md). Контракты: [cli.py](../../../autostop_manager/cli.py), [mcp_server.py](../../../autostop_manager/mcp_server.py), [mcp_tools.py](../../../autostop_manager/mcp_tools.py), [mcp_contract.py](../../../autostop_manager/mcp_contract.py), [manifest](../manager_mcp_catalog.json).

## MCP и ревизии

Native MCP работает на loopback `http://127.0.0.1:41931/mcp`, stateless Streamable HTTP. `build_server()` регистрирует инструменты и до открытия порта проверяет имена/fingerprint. CRM Gateway — отдельный MCP; его инструменты не входят в Manager manifest. Актуальная input schema берётся из всех страниц живого `tools/list`, а source fingerprint — из manifest; при изменении сигнатуры обновляй оба вместе с кодом.

Проверка цепи: active revision → initialize/ping → paginated tools/list → schema/annotations → безопасный bounded вызов → downstream. HTTP 200, количество tools или зелёный CI по отдельности не доказывают цепь. `mcp-probe` сверяет `readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint` для `manager_automations`, `manager_automation_control`, `j1_research_start`, `j1_research_vin`, `j1_research_record_facts`, `j1_research_status`, `j1_research_results`, `j1_research_document`, `j1_research_report`, `j1_research_add_queries`, `j1_research_cancel`, `store_digest`, `store_management_action`, `store_quote_conductor`; fingerprint учитывает только input schemas.

Перед live-операцией разреши `readlink -f /opt/autostop-manager-releases/current`, прочитай `REVISION` и используй документы/manifest этого физического snapshot. Для разработки зафиксируй путь checkout/worktree, `git rev-parse HEAD` и `git status --short`; используй проверенную development venv, не создавая её копию только ради чистого worktree. При смешанной задаче укажи оба корня и ревизии; выбирать инструкции можно абсолютными путями без смены cwd и runtime-копий. Для imports production probe запускается из `/tmp` с `PYTHONSAFEPATH=1` и `PYTHONPATH`, равным выбранному физическому snapshot: cwd не должен подменять installed imports. Сверяй фактический manifest, не историческое число tools.

После обслуживания 2026-10-21 installed source — `/opt/autostop-manager-releases/20261020T195623Z-b467f8c7`, `REVISION=b467f8c7d76db2aa68ef52e667b093cd84aa78ef`. Шесть native служб используют сохранённый Python `/opt/autostop-manager-cold-releases/51752bdb2048e0012e23b10bc770ff59f781874a/venv/bin/python3.12`; его историческое имя не задаёт ревизию приложения. Source выбирается новым cwd/PYTHONPATH, а CRM получает тот же source через read-only mount. Этот Python и его base runtime удалять нельзя. Перед очередной операцией перепроверь effective `ExecStart`, `/proc/<PID>/cwd`, окружение и импорт `autostop_manager.__file__`; датированная запись не заменяет текущую проверку.

Сохраняются текущие версии и одна проверенная копия данных каждого проекта (Manager + CRM совместно), без обязательной предыдущей установки. Резервирование — [complete-backup.md](complete-backup.md), границы очистки и техническое хранение — [disk-maintenance.md](disk-maintenance.md).

`codex mcp list` показывает config, endpoint `tools/list` — схемы сервера, а declarations текущего хода — доступные клиенту инструменты; эти срезы могут различаться. После разрешённой активации вызови App Server `config/mcpServer/reload` с `params: null`; он лишь ставит refresh загруженных чатов в очередь. Reload и следующий ход не гарантируют обновление desktop declarations. В новом ходе сравни endpoint `tools/list`, thread-specific `mcpServerStatus/list` и фактические declarations. Reload не является read-only и ради аудита не выполняется. Установка и rollback: [deployment.md](deployment.md). Контракт API: [Codex App Server](https://learn.chatgpt.com/docs/app-server#api-overview).

Для content-free проверки существующего work Telegram чата используй `WakeConfig.load()` и `AppServer` из active bridge release. Root-only `wake.json` даёт thread ID; его не выводи. `AppServer.manager_mcp_schema_status(expected_schemas, declarations=...)` использует `threadId`, `serverName="autostopmanager"`, `detail="toolsAndAuthOnly"` и все страницы `cursor=nextCursor`. Передай input schemas из endpoint и отдельно реально доступные клиенту declarations; без последних результат явно `model_declarations_unverified`. `runtimeStatus="connected"` и `toolsError=null` могут сопровождать cached catalog, поэтому сами по себе обновление не доказывают. При `model_declarations_stale` не подгоняй аргументы под старую схему. Для уже разрешённого bounded вызова доступен существующий App Server `mcpServer/tool/call` с `threadId`, `server`, `tool`, `arguments`, либо обычный MCP SDK через актуальный endpoint; это обход cached declaration, а не доказательство её обновления. Схему локального App Server уточняй через `codex app-server generate-json-schema --out <temporary-dir> --experimental`. Не перезапускай daemon ради schema mismatch.

Каждый выполненный Manager MCP tool, включая отдельные E15 search/excerpt/browser calls, получает `tool_execution`: случайный технический `call_id`, `started_at`, `ended_at`, `wall_ms`, отдельные backend/processing elapsed и известные network/attempt counts. Неизвестные backend значения — `null`, существующий `execution` сохраняется; технический outcome ограничен `ok/error/unknown`, доменный outcome остаётся в исходном ответе. Безопасные `mcp_tool_start`/`mcp_tool_end` JSON events пишутся только в stderr/journal, без аргументов, URL, клиентских идентификаторов, хешей, provider payload и текста исключений. Это время внутри инструмента; полный ход Codex измеряется по внешним `task_started` → `task_complete` (или явно указанным user-message → final границам), включая чтение инструкций и финальный ответ.

## Ограничения выполнения

Самостоятельный [VIN-режим J1](j1-vin-research.md) использует существующий worker и отдельное
приватное tmpfs-хранилище; сбор не блокирует MCP до завершения исследования.
`j1_research_vin`/`j1_research_record_facts` всегда зарегистрированы. Все восемь VIN-aware
research tools имеют readOnly=false/destructive=true/idempotent=false: VIN entrypaths выполняют
expiry cleanup, lost-state stub updates и failed-worker reconciliation, также перед отказом
отключённых VIN-записей. Status/results/report могут создать/мигрировать обычную SQLite, но не
получают новые страницы. VIN/add_queries имеют openWorld=true, остальные VIN-aware tools — false.
Общий J1/E15 VIN не принимает; general start сохраняет прежние annotations.
`j1_research_document` добавляет `page`/`ocr`; у обычной job прежние defaults сохраняются,
а активные page/OCR отклоняются. OCR ставится в дедуплицированную worker-очередь и меняет временные
revision/draft без новой сети. Domain receipts/CAS и selected-page dedup сохраняются, но не
делают lifecycle housekeeping идемпотентным; start key после expiry может создать новую job.
Источник данных не может менять job scope или получателей.

Флаг `AUTOSTOP_J1_VIN_RESEARCH_ENABLED=0` — значение по умолчанию. При отдельно порученной активации
Manager MCP и J1 worker читают один приватный `/etc/autostop-j1-vin.env`.
Оба используют root-owned `0700` RuntimeDirectory `/run/autostop-j1-vin`, preserve=yes и shared
ReadWritePaths. Runtime требует tmpfs и лимит 128 MiB; постоянная J1 DB содержит только обезличенный stub.
Restart worker сохраняет срок, host reboot возвращает `vin_ephemeral_state_lost` и не восстанавливает VIN.
`j1_vin_store.runtime_status()` — content-free локальная проверка enabled/готовности/сроков,
без старта поискового задания; общий J1 probe включает эту проверку.
Существование каталога, feature flag и зелёный source test не заменяют runtime-проверку.

Приёмка нового маршрута: отдельные состояния сбор/анализ, чтение цитируемой страницы,
запись claims с server-derived match evidence, отсутствие decoder API, финализация общей ревизии,
честные family/unknown/conflicts и expiry приватных данных. Для проверки источников нужен
отдельно разрешённый bounded случай; обязательные CI-тесты используют синтетические fixtures.
Изменение units/installers в Git не устанавливает службы. Граница PR/CI и выпуска — [deployment](deployment.md).

Выбранные блокирующие read-команды из [BLOCKING_READ_TOOLS](../../../autostop_manager/mcp_read_executor.py)
исполняются вне event loop: общий пул
на процесс — два workers и два ожидающих вызова, максимум 60 секунд ожидания.
Busy/timeout возвращают безопасные `error`, `stage`, `adapter_started` и
`adapter_may_continue`, без автоматического повтора. Отмена ожидания не доказывает
остановку потока; его слот удерживается до реального завершения. Статические
правила и сверка готовых данных остаются локальными вызовами.

Все десять Store entrypoints выполняются вне MCP event loop через
[Store executor](../../../autostop_manager/mcp_store_executor.py).
Runtime/search/entity/photo, analytics и owner capabilities используют отдельные
два workers, два ожидающих вызова и 60 секунд ожидания. Digest, management action,
owner API и quote conductor могут менять Store или Manager state; их отдельный
пул имеет один worker, два ожидающих вызова и 90 секунд ожидания. Это сохраняет
последовательность операций с состоянием и оставляет чтения для проверки результата.
Сигнатуры и завершённые доменные ответы сохраняются.

Отказы Store executor сохраняют семейство ответа и код `store_executor_busy`,
`store_executor_wait_timeout`, `store_executor_unavailable` или `store_executor_failed`.
Автоповтора нет. До старта adapter `request_dispatched=false` и
`outcome_uncertain=false`; после старта точный dispatch неизвестен
(`request_dispatched=null`). Для начавшейся операции с состоянием отказ означает
`status=compensating`, `outcome_uncertain=true`, `readback_required=true` и
`readback_verified=false`, даже если worker уже завершился к моменту отказа caller.
Сначала проверь точный объект/receipt; timeout не разрешает повтор записи.
Отмена queued-вызова не запускает adapter. Начавшийся worker удерживает слот до
фактического завершения и продолжает внутренний readback; отмена caller его не
останавливает. Эти ограничения ожидания не заменяют deadline самого HTTP transport.

`search_web_multi`, `fetch_page_excerpt`, `fetch_page_browser`,
`collect_work_price_evidence`, `estimate_repair_work_cost` и `verify_oem_candidates_web`
используют отдельный общий веб-пул: два workers, два ожидающих вызова и максимум
90 секунд ожидания. Отказ admission/worker сохраняет семейство ответа инструмента:
у E15 — web-формат `schema/capability/read_only` и
`error={code,retryable}`; `retryable=false`, исходные непроверенные аргументы не
возвращаются. Код причины — `provider_busy`, `provider_wait_timeout`,
`provider_executor_unavailable` или `provider_execution_failed`.
Collector сохраняет automotive envelope; estimator — плоский ответ оценки;
OEM verifier — `OemCandidatePublicEvidenceV1`. У этих трёх инструментов отказ
до старта worker возвращает `execution.network_calls=0` и `attempts=[]`.
После старта worker оба поля равны `null`: timeout или исключение не доказывают
отсутствия уже начатых HTTP reads. Непроверенные аргументы и тексты исключений
в ответе и telemetry отсутствуют; queued abandoned работа не запускает адаптер.

`collect_work_price_evidence.deadline_seconds` ограничивает ожидание admission
и acquisition одним исходным абсолютным сроком: время в очереди расходует бюджет,
а worker получает только оставшееся время. Пороги `[0.1,60]`, default 20 секунд
и `max_queries` 1–4 сохраняются. Pure/provided aggregate режимы тех же roots
также проходят admission, сохраняя отсутствие acquisition.
Общий медленный HTTP reader labor/research ещё не гарантирует жёсткий wall deadline
на заголовки и всё тело. При его зависании MCP остаётся отзывчивым, caller получает
timeout, а занятый worker удерживает слот до реального завершения.

`manager_automations` и `manager_automation_control` имеют независимые два workers,
два ожидающих вызова и 30 секунд ожидания, чтобы зависший веб-источник не мешал
управлению. Отмена и timeout также сохраняют занятый слот до завершения backend;
отменённый ожидающий вызов не начинает backend. Ошибка очереди управления сохраняет
`automation_control_unavailable`; поля `adapter_started` и `adapter_may_continue`
показывают, могло ли действие начаться. Не повторяй неизвестный исход мутации:
сначала проверь результат, затем используй прежнее тело и ключ идемпотентности.

MCP проверяет `manager_automation_control.expected_revision` как исходное JSON-число
целого типа: bool и строки не превращаются в revision. У `decode_vehicle_identity`
строгие поля `model_year`, `production_year`, `transmission_speeds`, `source_confidence`
сохраняют исходный тип, включая различие между JSON `null` и строкой `"null"`.
`collect_work_price_evidence.max_queries` тоже требует исходный JSON integer;
bool, float, строка и `null` отклоняются до исполнения. JSON-строки вложенных
`payload`, `options`, `work_items`, `vehicle_context` и `aggregate_evidence`
остаются совместимыми с SDK. Публичные input schemas/fingerprint сохраняются.

В `mcp-probe` timeout ограничивает handshake и весь обход `tools/list`, включая
ожидание отправки запроса и ответа. Обход принимает до 64 страниц, отклоняет
повтор cursor и превышение общего deadline; отдельная browser-проверка сохраняет
90 секунд. Manifest требует уникальные непустые строковые имена и целочисленный
count без приведения bool, чисел и строк к другому типу.

Синхронные vPIC и каталожные HTTP-чтения имеют общий deadline для заголовков
и тела, лимит размера и два transport workers на процесс. Незавершённый DNS
cleanup удерживает слот после timeout; `provider_transport_busy` не повторяется.

### Завершение Store перед остановкой Manager

Native HTTP-приложение имеет собственные Store pools и три отслеживаемых HTTP
слота. Управление не добавляет MCP tools и не меняет manifest:

- `GET /internal/store-workers` возвращает `store_worker_lifecycle_v1`,
  `instance_id`, `pid`, `accepting`, `status`, `drained` и `pending` отдельно для
  `read`, `stateful`, `transport`. Ответ не содержит аргументов и бизнес-данных.
- `POST /internal/store-workers/drain` с пустым телом и заголовком
  `X-Autostop-Store-Instance: <instance_id>` необратимо закрывает admission обоих
  Store pools. Ответ `202` означает продолжающийся drain, `200` — завершение.
  Повтор для того же instance идемпотентен; отсутствующий/чужой instance даёт `409`.
- Новые Store calls получают `store_executor_draining` до запуска adapter:
  `request_dispatched=false`, `outcome_uncertain=false`, без повтора. Уже принятые
  вызовы продолжают работу, включая внутренний POST/readback. Отменённые и
  просроченные queued calls по-прежнему не запускают adapter.

Оба endpoint доступны только прямому loopback caller с loopback Host и фактическим
портом listener. Origin и Forwarded/X-Forwarded-* отклоняются, CORS не включён,
ответы имеют `Cache-Control: no-store`. Это локальный интерфейс в границе доверия
native MCP, а не отдельная аутентификация root.

Контроллер до SIGTERM закрывает внешние источники работы, получает instance и
начинает drain, затем в ограниченный срок опрашивает GET того же instance.
`accepting=false` и `drained=true` требуют нуля реальных adapter jobs и HTTP workers,
включая очистку/DNS после timeout caller. HTTP admission закрывается только после
завершения всех принятых workflows, чтобы сохранить их readback. При превышении
срока admission остаётся закрытым; остановка/переключение релиза не разрешается.
Перед сигналом отдельно проверяются MainPID/start identity и installed snapshot.
Новый процесс получает новый instance. Resume и force-stop здесь отсутствуют.

Внешний ASGI shutdown имеет запасное ожидание 10 секунд и явно пишет
`store_drain_timeout` с pending counts при незавершении. Uvicorn до него уже ждёт
HTTP handlers; этот hook не заменяет pre-stop handshake и не гарантирует завершение
в `TimeoutStopSec=20s`. Thread/DNS принудительно не прерываются. `drained` описывает
только Store execution: это не успех бизнес-записи, не `readback_verified` и не
готовность всего Manager/CRM к выпуску. Подключение handshake к поддерживаемому
release controller выполняется отдельным изменением по [deployment](deployment.md).

## CLI

Точные аргументы: `python -m autostop_manager.cli <command> --help`.

| Команда | Назначение и эффект |
| --- | --- |
| `vehicle-tool`, `e4` | Один выбранный независимый VIN/frame/WMI инструмент; JSON аргументы через stdin, до 5 MiB. `e4` — исторический CLI alias, не текущий модуль каталогов E4. Использует действующие сигнатуры automotive tools, без повторной регистрации MCP, собственного runtime или неявного fallback. |
| `knowledge-sync`, `knowledge-audit` | Read-only аудит документов; название sync не означает запись. `--project-only` проверяет проект без наличия локально установленных файлов Codex. Требуются `ok=true`, отсутствие warnings. |
| `doctor` | Локальная диагностика; `--project-only` проверяет проект без внешних файлов Codex, `--integrations` читает live dependencies. `--full` создаёт тестовый workflow и требует одноразовых CRM/Store, не рабочую среду. |
| `store-conductor-release-gate` | Read-only проверка постоянного Store conductor state; disposable DB не заменяет этот gate перед выпуском. |
| `store-checkpoint-status` | `--stream store_digest|store_bootstrap`; читает существующую SQLite через `mode=ro`/`query_only`, без initialize/migration/repair. Current schema: exit 0, отсутствие строки — `exists:false`. Missing/unreadable DB, legacy/incomplete schema или corrupt state: диагностический JSON, exit 1. |
| `store-checkpoint-reset` | Запись: `--stream`, `--expected-state-version`, `--confirm-rebaseline`, `--reason cursor_generation_mismatch|cursor_ahead_after_store_restore|operator_verified_rebaseline`. Сначала сверка Store/state, потом независимый status. |
| `mcp-probe` | Handshake, manifest, annotations и bounded проверки. `--url`, `--timeout` (0..90], `--provider-failure-check`, `--store-check`, `--browser-check`; браузер читает example.com без сохранения текста в отчёт. |

`store-checkpoint-status` читает schema и строку в одной SQLite read transaction, включая последний committed WAL.
Ошибки возвращают `ok:false`, `read_only:true`, `stream` и `error`: `store_checkpoint_database_unavailable`
для missing/unreadable DB, `store_checkpoint_schema_unavailable` для отсутствующей/неполной checkpoint schema,
`store_checkpoint_state_unavailable` для corrupt DB или отказа чтения. Исправление schema выполняется отдельным writer.
Read-only означает отсутствие SQL-записей и изменений основной БД; SQLite может создавать/обновлять служебные WAL/SHM sidecars.

### Независимые VIN-команды

`python -m autostop_manager.cli vehicle-tool TOOL` читает один JSON object из stdin;
допустим `--input -`. Файл, пакетный shell dispatch и произвольное имя Python-функции
не принимаются. Имена и аргументы соответствуют текущим independent automotive
functions: `inspect_vehicle_identifier`, `decode_vin_vpic`, `decode_wmi_vpic`,
`decode_wmi_local`, `vininfo_decode`, `corgi_decode`, `vin_brand_details`,
`decode_frame_local`, `reconcile_vehicle_identity`, `decode_vehicle_batch`.
Контракты входов — [E2](../modules/E2.md) и текущие operation cards.

Запрос ограничен 5 MiB UTF-8 и глубиной JSON 64; лишние/пропущенные параметры, неверные типы,
duplicate keys и nonfinite JSON отклоняются до вызова backend. CLI не приводит
числовые строки или bool к числам. Ответ сохраняет действующий automotive result;
ошибки CLI содержат безопасный код без исходных аргументов и текста исключения.
Exit code: 0 при `ok=true`, иначе 1. `--help` не импортирует MCP, Store или декодеры.
Только явно выбранные online tools читают свой источник; локальный вызов и
reconciliation не добавляют сеть, базу Manager, установку пакетов или fallback.

Для подготовки VINinfo/Corgi сначала читай
`scripts/prepare-automotive-offline.py --help`: новый `--output PATH`, pinned
артефакты и отдельное поручение на подготовку. Проверка —
`scripts/check-automotive-offline.py --runtime EXACT_PATH`.
CLI не создаёт старый E4 cache и не предоставляет `e4-decoders prepare/status`;
`AUTOSTOP_AUTOMOTIVE_OFFLINE_RUNTIME` выбирает уже подготовленный attested runtime.

### Замещение исторического PR67

| Возможность PR67 | Действующий путь выпуска |
| --- | --- |
| Десять отдельных VIN-инструментов и MCP registration | Текущие `automotive_identity`/`automotive_offline`; одна регистрация в `automotive_mcp`, современные схемы сохранены. |
| JSON stdin CLI `e4 TOOL` | `vehicle-tool TOOL` и исторический alias `e4`, с современными аргументами; alias не меняет назначение модуля E4 «Каталоги». |
| Observation v1 и `identity_observations` | Действующие связанные automotive results, `reconcile_vehicle_identity` и готовый `vehicle_identity`; повторный decode не нужен. |
| Optional E4 cache, package installation и подготовка | Отдельные pinned prepare/check scripts, manifest, workers и attested offline runtime; второго decoder runtime нет. |
| Старые E4-инструкции, MCP count и CI optional setup | Актуальные E2/E4, manifest и release gates; устаревшие схемы и регистрация не возвращаются. |
| Старые partial-VIN/batch параметры и inspection diagnostics | Сохранены текущие native сигнатуры и диагностика составного `decode_vehicle_identity`; CLI не расширяет источник или текущий batch контракт. |

## Эффекты инструментов

Полный список имён — manifest; для задачи читай [A1](../modules/A1.md) и нужный модуль. Не вызывай изменяющий инструмент ради smoke: используй schema, synthetic fixture или явно безопасный dry-run в одноразовой среде.

- `manager_automations` читает техническое состояние: default/status/readiness могут инициализировать registry и обновить schema metadata текущей схемы. Миграция старой схемы добавляет/заполняет технические поля jobs/runtime/controller, включая schedule_timezone и applied_at. `templates` возвращает allowlisted шаблоны без доступа к registry. `manager_automation_control` меняет jobs/timers через guarded операции, а `run_now/test_notification` могут отложенно отправить Telegram. `preview` существующего job также может инициализировать registry, мигрировать технические поля или обновить schema metadata; preview создания из template не обращается к registry. Status/readiness/preview не выполняют предлагаемое guarded действие, не меняют desired_state/revision заданий, не ставят сообщения в очередь и не отправляют их. Native `manager_automations` имеет `readOnlyHint=false` и `idempotentHint=false`: технические SQLite-записи и повторное обновление schema timestamp учитываются для всего инструмента. Native `manager_automation_control` также имеет `idempotentHint=false`: одинаковый preview существующего job повторно обновляет schema timestamp. Guarded mutation keys/CAS/receipts, schemas, выходные форматы и полномочия controller сохраняются.
- Store `store_digest` читает события и меняет Manager cursor/ack; без кейса не продвигай production checkpoint. `store_management_action`, `store_quote_conductor`, `store_owner_api` различаются по операции и могут писать/публиковать; нужен текущий target, revision, contract, idempotency и readback. `prepare_action_contract` локально проверяет вход и возвращает JSON `ActionContractV2` без сети, записи или guarded token. Полномочия, текущую ревизию и предусмотренные guard/proof проверяет конкретный исполнитель по своему контракту. Подробности: [store-api.md](store-api.md).
- `download_store_quote_vin_photo` читает точный Store quote и возвращает bounded JPEG preview в `data.content_base64`/`content_type`, без локального файла. `expected_photo_sha256`/`summary.attachment.sha256` относятся к исходному вложению; `preview_sha256` — к возвращённым preview-байтам и может отличаться. Telegram download создаёт приватный временный файл по своему контракту. Фото остаются транзитными; отдельно сохранённый Store preview сверяй по `preview_sha256` и очищай. Подробности: [store-api.md](store-api.md).
- VIN/PartsAPI/каталоги читают источники и могут расходовать provider quota. Dry-run/configured не доказывает live доступ. OEM и аналоги остаются кандидатами до проверки применимости. Старый PartsAPI category index — fixture, не активный API путь.
- `elcats_catalog_query` — independent E4 operation на готовом профиле: default-disabled, page_budget=12/max24 и общий deadline45s. Robots/redirect/images входят в тот же бюджет; per-brand matrix и partial OCR evidence — [Elcats](elcats.md). Provider status показывает локальные возможности без probe; рабочая навигация и подтверждение номера различаются.
- Служебные VIN/каталожные методы: `benchmark_vin_parts_lookup` проверяет пачку идентификаторов, по умолчанию может читать vPIC; PartsAPI live-вызовы включаются отдельными флагами, отчёт скрывает исходные идентификаторы. `recommend_automotive_sources` выбирает технические источники без копирования лицензированных материалов; `lookup_public_automotive_evidence` читает публичные model-level recalls/TSB и маршруты по жидкостям, не подтверждает VIN-кампании или применимость. Сценарий — [E1](../modules/E1.md), контракты — текущие schemas и [mcp_tools.py](../../../autostop_manager/mcp_tools.py).
- `search_partsapi_category_index`, `explain_partsapi_category_for_intent`, `validate_partsapi_category_index` читают/проверяют исторический category fixture; не выбирай эти категории для текущего 43-методного контракта. [PartsAPI](partsapi.md).
- `j1_research_start/add_queries/cancel` меняют временную job/cache. Публичную сеть затем читает отдельный worker исследования. Работай с точным job ID; чужие jobs не подходят для smoke. Статус/материалы/отчёт читаются порциями. [web-research.md](web-research.md).
- `assess_part_market` и `assess_avito_price_sample` оценивают переданную выборку, не собирают предложения и не публикуют цену клиенту. [part-market.md](part-market.md), [market-listings.md](market-listings.md).
- `parts_store_cards` читает или меняет только колонку `parts_store`; `create/append_note` требуют guarded write и Gateway readback. [crm-mail.md](crm-mail.md).

## Скрипты и службы

Пути ниже относительно checkout. Parser/`--help` читай до выполнения. Installer выполняется из разрешённого active immutable release либо orchestrator с rollback; отсутствие `--activate` не делает его read-only.

| Скрипт | Вход и эффект |
| --- | --- |
| `scripts/doctor.sh` | Wrapper передаёт аргументы в `doctor`; обычный режим read-only, `--full` требует одноразовой среды. |
| `scripts/release-gates.sh` | Локальные проверки с disposable данными, не deploy. |
| `scripts/check-e2-branch-coverage.py` | Читает coverage JSON; требует 82% ветвей исторического E2 gate для catalog_clients/vin_oem_resolver/benchmark, не меняет данные. |
| `scripts/check-automotive-catalog.py` | Disposable state: сверяет registry, exact native schemas, все 43 PartsAPI и MD references; providers не вызывает. |
| `scripts/generate-automotive-instructions.py` | Создаёт operation MD из registry/schema; `--check` сравнивает без записи. |
| `scripts/export-automotive-tools.py` | `--revision EXACT_PUBLISHED_SHA --output PATH`: экспорт immutable Git snapshot в self-contained CRM bundle; сеть и business state не используются. |
| `scripts/prepare-automotive-offline.py` | Административная подготовка pinned VINinfo/Corgi в новый `--output PATH`; проверяет архивы, DB, licenses и Node; active current/services не меняет. |
| `scripts/check-automotive-offline.py` | `--runtime EXACT_PATH`: реальные public/synthetic positive/negative offline probes; данные клиентов не читает. |
| `scripts/automotive-vininfo-worker.py` | Внутренний bounded Python worker; public decoder читает VIN по stdin и attested package path; сеть запрещена. |
| `scripts/automotive-corgi-worker.mjs` | Внутренний Node worker; stdin и readonly pinned SQLite; сеть/download/fallback отсутствуют. |
| `scripts/vin-retest.py` | Только отдельно порученный live read-only benchmark native MCP на localhost: VIN из stdin, `--output-dir PRIVATE_PATH [--timeout 25] [--full-control]`; без CRM/Store-записей, скачивания баз и установки пакетов. Сохраняет обезличенный receipt без VIN, его хеша, профиля или provider payload. Process deadline и контроль представления — [стенд VIN](vin-retest.md). Самостоятельный J1 VIN-режим выбирается отдельно. |
| `scripts/install-manager-mcp.sh` | Runtime installer: `--activate [--replace-unit]`. |
| `scripts/install-manager-automation.sh` | Registry/unit/socket: `--manager-revision SHA`, необязательные `--crm-revision SHA`, `--crm-version`, `--activate`, `--replace-unit`. После попытки активации ошибка требует ручного восстановления: автоматического возврата прежнего writer нет. |
| `scripts/ensure-automation-group.sh` | Изменяет системную группу/UID-GID; вызывается installer. |
| `scripts/backup-manager-automation-state.py` | Online backup: `--output PATH [--source PATH]`, root-owned `0700`. |
| `scripts/autostop-complete-backup.py` | Прежний complete-bundle helper для исторических manifests; с текущим Store helper не запускается. Действующие копии и восстановление — [правила backup](complete-backup.md). |
| `scripts/store-backup.py` | Root-only текущая копия Store DB, files и settings; `--root` и `--lock` задают точные пути. Проверяет dump и стабильность источника, затем атомарно публикует одну генерацию. Live restore не выполняет. |
| `scripts/technical-retention.py` | Без `--apply` планирует уборку; `--apply --days 3 --docker` ограничивает только закрытые технические логи и разрешённые download/build caches. Busy/open guards сохраняют активные файлы; business data не входит. |
| `scripts/autostop-disk-maintenance.py` | Root-only `plan` пишет private manifest; `apply --manifest PATH --approve SHA256` удаляет проверенных кандидатов; `maintain` исполняет принятую policy; `pg-retain` согласует три дня PG; `recover-ci` возвращает собственную паузу CI. [Контракт очистки](disk-maintenance.md). |
| `scripts/install-autostop-disk-maintenance.py` | `--source-commit SHA --policy-file PRIVATE_JSON --receipt NEW_PRIVATE_PATH` устанавливает frozen root helper и включает два maintenance timers с сохранением/rollback конфигурации. Прикладной runtime не выпускается. |
| `scripts/install-j1-worker.sh` | Runtime installer: `--activate [--replace-unit]`. |
| `scripts/install-j1-browser-stack.sh` | Docker/unit installer: `--activate`, `--verify`, `--replace-unit`; memory/swap gate. |
| `scripts/run-j1-browser-stack.sh` | `revision` read-only; `start|stop` меняют containers. |
| `scripts/attest-j1-browser-stack.sh` | Создаёт release-bound marker после topology probe. |
| `scripts/install-telegram-bridge.sh` | Account release: `--account personal|work --revision <commit>`. |
| `scripts/provision-telegram-transcription-model.sh` | Подготавливает voice model: `--account work --revision <commit>`. |
| `scripts/deploy_telegram_bridge.sh` | Меняет release/service: work требует `--account work --no-start [revision]`; personal — `--account personal [revision]`, `--no-start` для него запрещён. |
| `scripts/install-codex-wake.sh` | Меняет unit/chat/config с backup/rollback; без аргументов. |
| `scripts/authorize-telegram-account.sh` | Интерактивная авторизация `--account personal|work`. |
| `scripts/set-work-telegram-duty.sh` | `--status` read-only; `--enable|--disable` меняют duty. |
| `scripts/work-telegram-python.sh` | `bridge|wake` читает effective systemd ExecStart и возвращает действующий Python; окружение не устанавливает, fallback на checkout отсутствует. |
| `scripts/export-work-telegram-inbox.py` | Экспортирует точные legacy-ссылки в новый root-private файл до согласованного переключения; без копирования переписки или replay. |
| `scripts/run-work-telegram-media.sh` | Приватный временный файл: `transcribe --file EXACT_FILE [--language ru] [--delete-after]`, `preview --file EXACT_FILE [--delete-after]` или `self-check`; `--language` доступен только `transcribe`. |
| `scripts/run-work-telegram-monitor-voice.sh` | Текущий event: `telegram_monitor_voice --event-id ... [--language ru]`, затем очистка. |
| `scripts/import_offline_parts_catalogs.py` | `--verify-only` read-only; иначе импорт `--archive ZIP --cache-root PATH`. |
| `scripts/ocr_offline_parts_catalogs.py` | `--verify-only` read-only; иначе OCR `--cache-root PATH`. |
| `scripts/sync_offline_parts_catalog_release.py` | `--verify-only` read-only; иначе pinned download/import/OCR до maintenance. |
| `scripts/remove-learning-hooks.py` | Dry-run по умолчанию; `--apply` меняет Codex config с backup только при необходимой legacy migration. |
| `scripts/update-instruction-catalogs.py` | Без флагов записывает A4/A5 по действующим проектным инструкциям и manifest установленных выбранных пакетов. `--check --project-only` без записи проверяет проектную часть для CI/release gates без локальных пакетов Codex; полный `--check` дополнительно сверяет установленные выбранные навыки на текущем хосте. Отключённые навыки пропускаются по имени или пути. Пакет без навыков не подменяется старым кешем; несколько версий требуют выбора актуальной. Число выбранных входов может отличаться от числа включённых навыков текущего сеанса. |
| `scripts/m2-journal.py` | `start` и `append --record PRIVATE_JSON` пишут журнал вне Git и обновляют указатели; `state` полностью заменяет текущее состояние. `validate(record)` проверяет без записи. UUID/idempotency, [формат](m2-journal-record.schema.json) и порядок — [M2](../modules/M2.md). |
| `scripts/client-behavior-eval.py` | Offline `validate`, `list`, `prompt --case-id ID` и `score --results PRIVATE_JSON` для [36 синтетических клиентских сценариев](client-behavior-eval.md). Читает локальные файлы, печатает JSON; не выполняет планы, сетевые вызовы или рабочие записи. |

Automation backup читает точное имя исходного файла через escaped SQLite URI и закрывает соединения до публикации. Проверенная копия публикуется без замены существующего или конкурирующего output; временный файл и output находятся в одном каталоге, файловая система должна поддерживать hard links. Файл сохраняет режим `0600`, проверки SQLite и fsync. Ошибка после публикации не доказывает отсутствие output: перед повтором сверь точный путь, существующая копия не перезаписывается.

При ошибке automation installer откат сохраняет исходный код завершения. Неуспешное восстановление файлов или состояния службы сохраняет rollback backup и диагностический путь; частично восстановленный комплект не запускается заново. Если до установки служба была выключена, её остановка предпринимается и при ошибке восстановления. Успех команды не заменяет независимую проверку фактического runtime.

Units в `deploy/systemd/`: Manager MCP, scheduler, J1 static/browser, work/personal Telegram, Codex wake/start. `systemctl is-active` показывает процесс, не правильность его контракта. Службы и backup: [host-operations.md](host-operations.md); Telegram/G1: [telegram-runtime.md](telegram-runtime.md).

Диагностика: `transport_route_unavailable` → socket/port/unit; `transport_auth_failure` → transport permissions; `tool_not_registered` → client/endpoint revision; `schema_mismatch` → imported PYTHONPATH и SHA; `annotation_mismatch` → active annotations/revision; provider failure → отдельный downstream. Ошибка provider/auth не повод перезапускать MCP.
