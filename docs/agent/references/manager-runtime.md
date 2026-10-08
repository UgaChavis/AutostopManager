# AutoStop Manager: CLI, MCP и Codex

Модули: [A3 — CLI](../modules/A3.md), [D1 — MCP](../modules/D1.md), [D2 — знания](../modules/D2.md). Контракты: [cli.py](../../../autostop_manager/cli.py), [mcp_server.py](../../../autostop_manager/mcp_server.py), [mcp_tools.py](../../../autostop_manager/mcp_tools.py), [mcp_contract.py](../../../autostop_manager/mcp_contract.py), [manifest](../manager_mcp_catalog.json).

## MCP и ревизии

Native MCP работает на loopback `http://127.0.0.1:41931/mcp`, stateless Streamable HTTP. `build_server()` регистрирует инструменты и до открытия порта проверяет имена/fingerprint. CRM Gateway — отдельный MCP; его инструменты не входят в Manager manifest. Актуальная input schema берётся из всех страниц живого `tools/list`, а source fingerprint — из manifest; при изменении сигнатуры обновляй оба вместе с кодом.

Проверка цепи: active revision → initialize/ping → paginated tools/list → schema/annotations → безопасный bounded вызов → downstream. HTTP 200, количество tools или зелёный CI по отдельности не доказывают цепь. `mcp-probe` сверяет `readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint` для `j1_research_start`, `j1_research_add_queries`, `store_digest`, `store_management_action`, `store_quote_conductor`; fingerprint учитывает только input schemas.

Перед live-операцией разреши `readlink -f /opt/autostop-manager-releases/current`, прочитай `REVISION` и используй документы/manifest этого физического snapshot. Для разработки зафиксируй путь checkout/worktree, `git rev-parse HEAD` и `git status --short`; source-команды используют его собственную `.venv/bin/python`. При смешанной задаче укажи оба корня и ревизии; выбирать инструкции можно абсолютными путями без смены cwd и runtime-копий. Для imports production probe запускается из `/tmp` с `PYTHONSAFEPATH=1` и `PYTHONPATH`, равным выбранному физическому snapshot: cwd не должен подменять installed imports. Сверяй фактический manifest, не историческое число tools.

`codex mcp list` показывает config, endpoint `tools/list` — схемы сервера, а declarations текущего хода — доступные клиенту инструменты; эти срезы могут различаться. После разрешённой активации вызови App Server `config/mcpServer/reload` с `params: null`; он лишь ставит refresh загруженных чатов в очередь. Reload и следующий ход не гарантируют обновление desktop declarations. В новом ходе сравни endpoint `tools/list`, thread-specific `mcpServerStatus/list` и фактические declarations. Reload не является read-only и ради аудита не выполняется. Установка и rollback: [deployment.md](deployment.md). Контракт API: [Codex App Server](https://learn.chatgpt.com/docs/app-server#api-overview).

Для content-free проверки существующего work Telegram чата используй `WakeConfig.load()` и `AppServer` из active bridge release. Root-only `wake.json` даёт thread ID; его не выводи. `AppServer.manager_mcp_schema_status(expected_schemas, declarations=...)` использует `threadId`, `serverName="autostopmanager"`, `detail="toolsAndAuthOnly"` и все страницы `cursor=nextCursor`. Передай input schemas из endpoint и отдельно реально доступные клиенту declarations; без последних результат явно `model_declarations_unverified`. `runtimeStatus="connected"` и `toolsError=null` могут сопровождать cached catalog, поэтому сами по себе обновление не доказывают. При `model_declarations_stale` не подгоняй аргументы под старую схему. Для уже разрешённого bounded вызова доступен существующий App Server `mcpServer/tool/call` с `threadId`, `server`, `tool`, `arguments`, либо обычный MCP SDK через актуальный endpoint; это обход cached declaration, а не доказательство её обновления. Схему локального App Server уточняй через `codex app-server generate-json-schema --out <temporary-dir> --experimental`. Не перезапускай daemon ради schema mismatch.

Каждый выполненный Manager MCP tool, включая отдельные E15 search/excerpt/browser calls, получает `tool_execution`: случайный технический `call_id`, `started_at`, `ended_at`, `wall_ms`, отдельные backend/processing elapsed и известные network/attempt counts. Неизвестные backend значения — `null`, существующий `execution` сохраняется; технический outcome ограничен `ok/error/unknown`, доменный outcome остаётся в исходном ответе. Безопасные `mcp_tool_start`/`mcp_tool_end` JSON events пишутся только в stderr/journal, без аргументов, URL, клиентских идентификаторов, хешей, provider payload и текста исключений. Это время внутри инструмента; полный ход Codex измеряется по внешним `task_started` → `task_complete` (или явно указанным user-message → final границам), включая чтение инструкций и финальный ответ.

## Ограничения выполнения

Выбранные блокирующие read-команды из [BLOCKING_READ_TOOLS](../../../autostop_manager/mcp_read_executor.py)
исполняются вне event loop: общий пул
на процесс — два workers и два ожидающих вызова, максимум 60 секунд ожидания.
Busy/timeout возвращают безопасные `error`, `stage`, `adapter_started` и
`adapter_may_continue`, без автоматического повтора. Отмена ожидания не доказывает
остановку потока; его слот удерживается до реального завершения. Статические
правила и сверка готовых данных остаются локальными вызовами.

Синхронные vPIC и каталожные HTTP-чтения имеют общий deadline для заголовков
и тела, лимит размера и два transport workers на процесс. Незавершённый DNS
cleanup удерживает слот после timeout; `provider_transport_busy` не повторяется.

## CLI

Точные аргументы: `python -m autostop_manager.cli <command> --help`.

| Команда | Назначение и эффект |
| --- | --- |
| `knowledge-sync`, `knowledge-audit` | Read-only аудит документов; название sync не означает запись. `--project-only` проверяет проект без наличия локально установленных файлов Codex. Требуются `ok=true`, отсутствие warnings. |
| `doctor` | Локальная диагностика; `--project-only` проверяет проект без внешних файлов Codex, `--integrations` читает live dependencies. `--full` создаёт тестовый workflow и требует одноразовых CRM/Store, не рабочую среду. |
| `store-conductor-release-gate` | Read-only проверка постоянного Store conductor state; disposable DB не заменяет этот gate перед выпуском. |
| `store-checkpoint-status` | Read-only checkpoint: `--stream store_digest|store_bootstrap`. |
| `store-checkpoint-reset` | Запись: `--stream`, `--expected-state-version`, `--confirm-rebaseline`, `--reason cursor_generation_mismatch|cursor_ahead_after_store_restore|operator_verified_rebaseline`. Сначала сверка Store/state, потом независимый status. |
| `mcp-probe` | Handshake, manifest, annotations и bounded проверки. `--url`, `--timeout` (0..90], `--provider-failure-check`, `--store-check`, `--browser-check`; браузер читает example.com без сохранения текста в отчёт. |

## Эффекты инструментов

Полный список имён — manifest; для задачи читай [A1](../modules/A1.md) и нужный модуль. Не вызывай изменяющий инструмент ради smoke: используй schema, synthetic fixture или явно безопасный dry-run в одноразовой среде.

- `manager_automations` (`status/templates/readiness`) читает техническое состояние; `manager_automation_control` меняет jobs/timers, а `run_now/test_notification` могут отложенно отправить Telegram. `preview` не пишет registry.
- Store `store_digest` читает события и меняет Manager cursor/ack; без кейса не продвигай production checkpoint. `store_management_action`, `store_quote_conductor`, `store_owner_api` различаются по операции и могут писать/публиковать; нужен текущий target, revision, contract, idempotency и readback. `prepare_action_contract` создаёт guarded token, не бизнес-запись. Подробности: [store-api.md](store-api.md).
- `download_store_quote_vin_photo` и Telegram download создают приватный временный файл: только точный разрешённый target, сверка SHA и очистка. Обычный read-only не означает отсутствие приватных данных.
- VIN/PartsAPI/каталоги читают источники и могут расходовать provider quota. Dry-run/configured не доказывает live доступ. OEM и аналоги остаются кандидатами до проверки применимости. Старый PartsAPI category index — fixture, не активный API путь.
- Служебные VIN/каталожные методы: `benchmark_vin_parts_lookup` проверяет пачку идентификаторов, по умолчанию может читать vPIC; PartsAPI live-вызовы включаются отдельными флагами, отчёт скрывает исходные идентификаторы. `recommend_automotive_sources` выбирает технические источники без копирования лицензированных материалов; `lookup_public_automotive_evidence` читает публичные model-level recalls/TSB и маршруты по жидкостям, не подтверждает VIN-кампании или применимость. Сценарий — [E1](../modules/E1.md), контракты — текущие schemas и [mcp_tools.py](../../../autostop_manager/mcp_tools.py).
- `search_partsapi_category_index`, `explain_partsapi_category_for_intent`, `validate_partsapi_category_index` читают/проверяют исторический category fixture; не выбирай эти категории для текущего 43-методного контракта. [PartsAPI](partsapi.md).
- `j1_research_start/add_queries/cancel` меняют временную job/cache; start также читает публичную сеть. Работай с точным job ID; чужие jobs не подходят для smoke. Статус/материалы/отчёт читаются порциями. [web-research.md](web-research.md).
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
| `scripts/install-manager-mcp.sh` | Runtime installer: `--activate [--replace-unit]`. |
| `scripts/install-manager-automation.sh` | Registry/unit/socket: `--manager-revision SHA`, необязательные `--crm-revision SHA`, `--crm-version`, `--activate|--activate-under-hold`, `--replace-unit`, `--release-attempt-key`. |
| `scripts/ensure-automation-group.sh` | Изменяет системную группу/UID-GID; вызывается installer. |
| `scripts/backup-manager-automation-state.py` | Online backup: `--output PATH [--source PATH]`, root-owned `0700`. |
| `scripts/autostop-complete-backup.py` | `plan` проверяет ёмкость и runtime без записи; `create` создаёт локальную копию данных, `verify --backup PATH` проверяет её. `daily` сохраняет канонический PostgreSQL backup при отказе полной копии. Root-only данные, установка/активация отдельно: [правила backup](complete-backup.md). |
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
| `scripts/run-work-telegram-media.sh` | Приватный временный файл: `transcribe|preview --file EXACT_FILE [--language ru] [--delete-after]` или `self-check`. |
| `scripts/run-work-telegram-monitor-voice.sh` | Текущий event: `telegram_monitor_voice --event-id ... [--language ru]`, затем очистка. |
| `scripts/import_offline_parts_catalogs.py` | `--verify-only` read-only; иначе импорт `--archive ZIP --cache-root PATH`. |
| `scripts/ocr_offline_parts_catalogs.py` | `--verify-only` read-only; иначе OCR `--cache-root PATH`. |
| `scripts/sync_offline_parts_catalog_release.py` | `--verify-only` read-only; иначе pinned download/import/OCR до maintenance. |
| `scripts/remove-learning-hooks.py` | Dry-run по умолчанию; `--apply` меняет Codex config с backup только при необходимой legacy migration. |
| `scripts/update-instruction-catalogs.py` | Без флагов записывает A4/A5 по действующим проектным инструкциям и manifest установленных выбранных пакетов; `--check` сравнивает без записи. Отключённые навыки пропускаются по имени или пути. Пакет без навыков не подменяется старым кешем; несколько версий требуют выбора актуальной. Число выбранных входов может отличаться от числа включённых навыков текущего сеанса. |
| `scripts/m2-journal.py` | `start` и `append --record PRIVATE_JSON` пишут журнал вне Git и обновляют указатели; `state` полностью заменяет текущее состояние. `validate(record)` проверяет без записи. UUID/idempotency, [формат](m2-journal-record.schema.json) и порядок — [M2](../modules/M2.md). |

Units в `deploy/systemd/`: Manager MCP, scheduler, J1 static/browser, work/personal Telegram, Codex wake/start. `systemctl is-active` показывает процесс, не правильность его контракта. Службы и backup: [host-operations.md](host-operations.md); Telegram/G1: [telegram-runtime.md](telegram-runtime.md).

Диагностика: `transport_route_unavailable` → socket/port/unit; `transport_auth_failure` → transport permissions; `tool_not_registered` → client/endpoint revision; `schema_mismatch` → imported PYTHONPATH и SHA; `annotation_mismatch` → active annotations/revision; provider failure → отдельный downstream. Ошибка provider/auth не повод перезапускать MCP.
