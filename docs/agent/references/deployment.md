# Выпуск и восстановление AutoStop Manager

Выпуск и перезапуск выполняются по текущему явному поручению. Сохраняй исходники, рабочие изменения и историю; проверяй опубликованную и установленную ревизии отдельно. До действий выбери физический installed snapshot по `REVISION` для live-контракта и checkout/worktree по HEAD/diff для подготовки выпуска; не подменяй один корень другим. Границы хостов: [host-operations.md](host-operations.md).

## Публикация исходников

В отдельном worktree запускай `./scripts/release-gates.sh` с одноразовыми данными; минимальная coverage — 82%. Влей актуальный `origin/AutostopManager` в feature branch без переписывания истории, повтори gates, опубликуй и merge зелёного PR. Подтверди точный remote SHA через `git ls-remote`. CRM сохраняет свой Python registrar и guarded ledger; они не входят в native Manager MCP.

Если поручение ограничено готовым PR и зелёным CI, остановись на этой границе: merge,
установка/включение J1 VIN и серверный выпуск требуют отдельного объёма поручения.
Исходники новой функции, units/installers и обновлённые инструкции не доказывают работающий runtime.

## Отдельная активация VIN-исследования J1

Самостоятельный [VIN-режим](j1-vin-research.md) по умолчанию выключен,
но два новых инструмента включены в source manifest и регистрируются при любом значении флага.
При разрешённом будущем выпуске сохранить прежний SHA и конфигурацию; общий приватный
`/etc/autostop-j1-vin.env` должен задавать одинаковый флаг Manager MCP и J1 worker.
До включения проверить root-owned `0700` tmpfs `/run/autostop-j1-vin`, 128 MiB budget,
согласованные RuntimeDirectory/Preserve/ReadWritePaths и локальный VIN runtime status.
Worker restart не должен удалять действующую job; после host reboot ожидается
`vin_ephemeral_state_lost`, без восстановления полного VIN из постоянного stub.

После штатной активации сверить installed SHA, все страницы `tools/list`, schemas/annotations,
состояния сбора/анализа и полный отдельно порученный цикл до финализации/expiry.
VIN-bearing browser URLs остаются запрещены; прежняя browser-аттестация нужна лишь для
обезличенных страниц. При отказе выключить способность и остановить дальнейшую сеть VIN-jobs
в рамках разрешённого rollback; возвращать код без проверки runtime недостаточно.
Эта инструкция сама не поручает активацию, merge или изменение CRM/Store/VPN.

## Согласованный серверный выпуск

1. Выполни `.venv/bin/python -m autostop_manager.cli store-conductor-release-gate` на постоянном Store-состоянии; разберись с заблокированными legacy runs. Запусти `scripts/remove-learning-hooks.py` в dry-run; `--apply` используй только для требуемой legacy migration, сохраняя backup.
2. Чистый server checkout переключи на `AutostopManager`, fetch и fast-forward до опубликованного SHA. Проверь private Manager `.env`: loopback `AUTOSTOP_STORE_API_URL`, Store READ/MANAGE/QUOTE/OWNER tokens; окружение CRM отдельное. Проверяются имена и наличие, значения не выводятся.
3. Нормальный orchestrator — `/opt/autostopcrm/deploy.sh`. Он до maintenance выполняет [pinned catalog sync](offline-catalogs.md), затем backup релиза/config/DB, Automation hold, work-Telegram pause, immutable Manager activation, проверки и rollback. Не захватывай отдельный hold и не запускай installers поверх coordinated deploy; не переключай `current` вручную.
4. Native Manager MCP активируется по умолчанию (`AUTOSTOP_MANAGER_MCP_ACTIVATE_ON_DEPLOY=1`). Для static J1 используй `AUTOSTOP_J1_ACTIVATE_ON_DEPLOY=1`. Browser включается отдельно через `AUTOSTOP_J1_BROWSER_ACTIVATE_ON_DEPLOY=1`: нужны `MemAvailable >=2 GiB`, `SwapFree >=1 GiB`, нулевой swap I/O за 60 секунд и повторная проверка ёмкости внутри deploy. Отказ gate сохраняет static J1; marker вручную не создаётся.
5. Проверь installed Manager/CRM revisions, bounded MCP/CRM/Store probes, `doctor --integrations` без `--full`, восстановление duty и отсутствие retired integration-audit/watchdog units. Full doctor допустим лишь с одноразовыми CRM/Store. Для J1 нужны active `autostop-j1.service` и probe из [web-research.md](web-research.md).
6. Сверь установленный `AGENTS.md`, A1, M1/M2 и A4/A5 с опубликованной ревизией; запусти `scripts/update-instruction-catalogs.py --check` из установленного snapshot. Числа A4/A5 относятся к выбранным файлам, а не ко всем включённым навыкам реестра. Обнови числа и ссылки конструктора по этому срезу, независимо перечитай карту и сохрани положение модулей и связи.

Store выпускается отдельно через его GitHub `Deploy VPS` и текущий `/opt/autostopapp/docs/deploy_rollback.md`. Push/merge в Store `main`, включая docs-only, запускает полный cutover: сначала CI, backup/headroom/network/proxy gates, потом независимый readback. Не объединяй его с CRM cutover без согласованного объёма.

При изменении sourcing warning-контракта сначала установи и проверь совместимый Manager consumer, затем публикуй Store producer в `main`: отправка Store запускает выпуск автоматически. Порядок и текущие warning-коды — [store-api.md](store-api.md).

## Native MCP и подключённый Codex

Проверь active `autostop-manager-mcp.service`; cwd его `MainPID` должен совпадать с физическим target `/opt/autostop-manager-releases/current`. Probe запускай из `/tmp` с активным `PYTHONPATH`, чтобы checkout не подменил импорт:

```bash
manager_snapshot=$(readlink -f /opt/autostop-manager-releases/current)
env PYTHONSAFEPATH=1 PYTHONPATH="$manager_snapshot" /opt/AutostopManager/.venv/bin/python -m autostop_manager.cli mcp-probe --url http://127.0.0.1:41931/mcp --provider-failure-check --store-check
```

Требуются handshake, все страницы `tools/list`, manifest/schema и annotation parity, безопасные вызовы. `--store-check` читает health/capabilities и не более одного заказа через `store_search(entity="store_order", limit=1)` без pagination и записи. Проверяй `checks.store_order_search.ok=true`; `store_order_sample_empty` означает доступ без образца для проверки полей. Отчёт не сохраняет ID, поля или raw provider errors.

После `browser_ready=true` и `browser_containers_ready=true` повтори probe с `--timeout 90 --browser-check`; нужен `checks.fetch_page_browser.ok=true` для публичного `https://example.com/`. Container health не подтверждает всю Manager browser chain.

В существующем клиенте после активации отправь App Server `config/mcpServer/reload` (`params: null`), затем в новом ходе проверь `mcpServerStatus/list` и endpoint `tools/list`. Старый ход может сохранять прежние declarations. `partsapi_catalog_lookup` должен содержать `provider_parameters`, `supplier_id`; `norms_models` проверяется с lowercase make code сначала dry-run, затем разрешённым bounded catalog lookup. Подтверждение reload не заменяет вызов. Протокол: [OpenAI App Server](https://developers.openai.com/codex/app-server).

Для marketplace проверь `catalog_provider_status(stage="market_listing")` и четыре инструмента Avito/Drom. Avito: dry-run → bounded search → точное listing read. Drom запускается только после API access и `AUTOSTOP_DROM_LISTINGS_ENABLED=1`; queued task не равна результату. Подробности: [market-listings.md](market-listings.md). Auth/quota/provider ошибки отличай от transport. После проверок восстанови прежний work-mode.

## Новый вход в роль M2

После стабильной приёмки выпуска и обновления технического состояния создай новый ephemeral Codex thread с обычным cwd `/opt/AutostopManager` и поручением работать инженером M2. Сохрани штатные базовые инструкции. Передай явный физический installed root с `REVISION` и отдельный source root с HEAD/diff, без изменения cwd или копирования runtime. Ограничь проверку чтением installed `AGENTS.md`, A1, M1, M2 и постоянных состояния M2, индекса и последней сводки по индексу. Переписка, клиентские кейсы, секреты, изменяющие инструменты, journal start/append и запуск исполнителей в этот smoke не входят.

Приёмка требует фактических завершённых чтений и совпадения SHA-256 всех семи файлов с независимым срезом до и после хода, а также живого `tools/list` с текущим schema fingerprint и annotations. Ответ агента без этих receipts не подтверждает загрузку. Сохраняй только технические hashes, статусы и ограничения; polling делай порциями до 60 секунд, после хода отпишись от ephemeral thread.

Ошибка первого чтения остаётся в отчёте; она восстановлена только после успешного чтения нужного файла с совпавшим hash. Журналы и история читаются через актуальный индекс M2, без копирования прежних рабочих кейсов. Внешние ограничения доступа оценивай отдельно от успешного входа в роль.

## Standalone восстановление

Standalone изменения выполняй одной опубликованной ревизией с сохранённым rollback. `scripts/install-manager-mcp.sh --activate` допустим только в таком явно заданном scope, не после успешного coordinated deploy.

Для work Telegram останови duty и соблюдай порядок [telegram-runtime.md](telegram-runtime.md), включая обязательный `scripts/deploy_telegram_bridge.sh --account work --no-start <revision>` и `scripts/install-codex-wake.sh`; до включения проверь CRM/MCP, voice, systemd и версии без сообщений клиентам. Personal Telegram имеет отдельный release/session; его deploy запрещает `--no-start`.

Для Automation используй один release-attempt key: quiescent hold → online registry backup в root-owned `0700` → снимки unit/timer drop-ins/duty → install active immutable release under hold → scheduler/socket/readiness, пять timers, CRM/Telegram smoke → release hold. На ошибке сохраняй hold и паузу work, восстанавливай компоненты штатным rollback с независимой проверкой. Сохраняй volumes, uploads и registry. Старый registry нельзя восстановить поверх новых операций без отдельного решения о state recovery.
