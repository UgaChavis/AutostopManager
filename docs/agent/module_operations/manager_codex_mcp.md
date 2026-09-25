# Manager CLI, native MCP и Codex

Проверено по исходникам `4e26c6c336d9ff60770803e940860881cdd69615` 2026-09-25. Источники контрактов: [`cli.py`](../../../autostop_manager/cli.py), [`mcp_server.py`](../../../autostop_manager/mcp_server.py), [`mcp_tools.py`](../../../autostop_manager/mcp_tools.py), [`mcp_contract.py`](../../../autostop_manager/mcp_contract.py), [`manager_mcp_catalog.json`](../manager_mcp_catalog.json), [`mcp_release_checks.md`](../../mcp_release_checks.md). Данные CRM, Store и переписка принадлежат соответствующим системам; Manager хранит лишь ограниченное техническое состояние. Состояние production перечитывайте перед решением: снимок ниже сделан до выпуска этой ревизии.

## Маршрут и границы

`Codex → CLI` выполняет локальную диагностику; `Codex → http://127.0.0.1:41931/mcp → Manager` обслуживает native MCP. CRM Gateway — отдельный MCP, его инструменты не входят в Manager manifest. `mcp_server.build_server()` регистрирует инструменты через `register_manager_tools()` и до открытия порта проверяет имена и fingerprint схем. HTTP transport — stateless Streamable HTTP на loopback, endpoint `/mcp`. Для каждого MCP-вызова сначала проверяйте `initialize`, `ping`, все страницы `tools/list`, затем схему нужного инструмента и только после этого безопасный smoke. Число инструментов и HTTP 200 без схем и вызова не подтверждают работоспособность.

Для интерактивного клиента `codex mcp list` показывает настроенные серверы, но не доказывает загруженные в текущем ходе инструменты. После штатной активации отправьте App Server `config/mcpServer/reload` с `params: null`, затем перечитайте `mcpServerStatus/list` и `tools/list` в **новом** ходе. Текущий ход может удерживать старые declarations. Не меняйте конфиг или ссылки `current` ради локальной проверки. Скрипты `scripts/install-manager-mcp.sh` и `/opt/autostopcrm/deploy.sh` меняют runtime; их порядок и rollback описаны в [`deployment_runbook.md`](../deployment_runbook.md).

Точная проверка persistent work Telegram задачи через App Server 0.157.0 (без чтения её ходов/переписки) использует root-only `/etc/autostop-work-telegram/wake.json` лишь как источник thread ID. Команда ниже с аргументом `status` **read-only**; с `reload` отправляет поддерживаемый `config/mcpServer/reload` и предназначена **только после deploy**. Внешний MCP endpoint отдельно проверяйте `mcp-probe` из активной ревизии.

```bash
env PYTHONPATH=/opt/autostop-work-telegram-releases/current /opt/AutostopManager/.venv/bin/python - status <<'PY'
import asyncio
import sys
from autostop_manager.telegram_wake import AppServer, WakeConfig

async def main():
    config = WakeConfig.load()
    app = AppServer(config)
    try:
        await app.connect()
        if sys.argv[1] == "reload":
            await app.request("config/mcpServer/reload", None)
        elif sys.argv[1] != "status":
            raise ValueError("expected status or reload")
        cursor = None
        servers = []
        while True:
            params = {"threadId": config.thread_id, "detail": "toolsAndAuthOnly"}
            if cursor:
                params["cursor"] = cursor
            page = await app.request("mcpServerStatus/list", params)
            servers.extend(page["data"])
            cursor = page.get("nextCursor")
            if not cursor:
                break
        manager = next(item for item in servers if item["name"] == "autostopmanager")
        tools = manager["tools"]
        schema = tools.get("search_offline_parts_catalogs", {}).get("inputSchema", {})
        print({"connected": manager.get("runtimeStatus") == "connected",
               "tool_count": len(tools), "tools_error": bool(manager.get("toolsError")),
               "offline_search_schema_ok": schema.get("required") == ["query"]
               and {"query", "limit", "brand", "catalog_id"} <= set(schema.get("properties", {}))})
    finally:
        await app.close()

asyncio.run(main())
PY
```

После deploy смените в первой строке **только** `status` на `reload`, дождитесь обновления и повторите `status`: нужно `connected=true`, `tool_count=43`, `tools_error=false`, `offline_search_schema_ok=true` для опубликованной ревизии `4e26c6c`. Перед deploy 2026-09-25 эта команда read-only показала `connected=true`, 42 tools и отсутствие нового tool. App Server schema получена локально командой `codex app-server generate-json-schema --out <temporary-dir> --experimental`; `mcpServerStatus/list` требует `threadId`, принимает `detail=toolsAndAuthOnly` и возвращает paginated `data`/`nextCursor`. Совпадение task status не подтверждает declarations уже начатого хода: проверяйте новый ход отдельно.

Источник точной **текущей input schema каждого инструмента** — ответ `tools/list` живого endpoint. Сверка source идет через `docs/agent/manager_mcp_catalog.json` (имена, количество, SHA-256 отсортированных `{name,inputSchema}`), проверки `tests/test_mcp_tools.py`, `tests/test_mcp_contract.py` и `cli mcp-probe`. При изменении сигнатуры обновляйте manifest вместе с кодом. Просмотр конкретной схемы в подключенном Codex: найдите имя в `tools/list` и прочитайте `inputSchema`; не считайте старую карточку точнее активной схемы.

## CLI: все подкоманды

Рабочая папка для **source** проверок: `/opt/AutostopManager` или чистый worktree. Python: `/opt/AutostopManager/.venv/bin/python`. Для **production** probe запускайте из `/tmp` с `PYTHONPATH=/opt/autostop-manager-releases/current`, иначе `''` в `sys.path` импортирует код checkout и сравнит новый manifest со старым endpoint.

| Подкоманда `python -m autostop_manager.cli` | Вход, эффект и безопасная проверка |
| --- | --- |
| `knowledge-sync`, `knowledge-audit` | Без аргументов; read-only проверка локальной документации через `diagnostics.audit_documentation`; `ok=true` без `warnings`. Название `sync` не означает запись. |
| `doctor` | `--integrations` включает live-зависимости; `--full` расширяет проверки. Без флагов — локальная диагностика. Для production из активной ревизии используйте `doctor --integrations --full`; проверяйте отдельные поля, не только `ok`. |
| `store-conductor-release-gate` | Read-only gate сохранённого Store quote conductor; запуск из source с disposable `AUTOSTOP_MANAGER_DB` не заменяет официальный gate на постоянном состоянии. |
| `store-checkpoint-status` | `--stream store_digest|store_bootstrap`; read-only состояние checkpoint в `AUTOSTOP_MANAGER_DB`. |
| `store-checkpoint-reset` | Запись; требует `--stream`, `--expected-state-version`, `--reason cursor_generation_mismatch|cursor_ahead_after_store_restore|operator_verified_rebaseline`, `--confirm-rebaseline`. Только после точной сверки Store и checkpoint; затем независимый `status`. |
| `mcp-probe` | `--url` (по умолчанию loopback `/mcp`), `--timeout` (0..90], `--provider-failure-check`, `--store-check`, `--browser-check`. Synthetic read-only проверка handshake, paginated `tools/list`, manifest и ограниченных вызовов. Browser probe читает публичную `https://example.com/`; не сохраняет её текст в отчёте. |

Точная встроенная справка: `python -m autostop_manager.cli --help` и `... <подкоманда> --help`. Код: `cli.build_parser/main`; тесты: `tests/test_core_cleanup.py`, `tests/test_mcp_probe.py`, `tests/test_runtime_boundaries.py`. `--help` и тесты не доказывают доступность production downstream.

## MCP-инструменты: маршрут проверки

Все 43 имени source revision содержатся в manifest; у production-снимка 2026-09-25 было 42 имени. Ниже каждая строка задаёт владельца, эффект и короткий smoke. `read-only` не означает отсутствие сетевого чтения или записи в временный J1-кеш. **Ни один инструмент с потенциальной записью не вызывайте как smoke**: проверяйте его схему и synthetic/dry-run либо тест в disposable среде.

| Инструмент | Назначение, владелец данных, эффект и безопасный smoke |
| --- | --- |
| `manager_automations` | Automation Center: `status/templates/readiness`; технический read-only пакет. Smoke: `operation=readiness`, сверить `ready/checks` и reconciliation. |
| `manager_automation_control` | Preview или изменение allowlisted job/timer; запись требует idempotency key и существующий revision. Smoke: `operation=preview` с синтетическим target, либо только `tools/list`; `run_now` и `test_notification` имеют внешний эффект. |
| `get_store_analytics_report` | Агрегат Store, read-only; synthetic/ограниченный отчёт по активной схеме, без экспорта клиентских строк. |
| `store_owner_capabilities` | Store API права/контракт, read-only; smoke без аргументов. |
| `store_owner_api` | Store operations; read и write зависят от операции. Smoke: только capabilities/dry-run по Store карточке, никогда apply без точного target. |
| `store_runtime_status` | Store technical readiness, read-only; smoke без аргументов. |
| `store_digest` | Store события/сводка, read-only для Store; проверять bounded cursor и состояние, не копировать live записи в docs. |
| `store_search` | Поиск Store, read-only; synthetic невозможен как проверка качества реального каталога; schema и ограниченный разрешённый запрос. |
| `store_entity_context` | Точная сущность Store, read-only; только по текущему идентификатору кейса. |
| `download_store_quote_vin_photo` | Скачивание фото Store в ограниченный локальный временный файл; доступ по текущему quote target, после проверки удалить. Не synthetic smoke на production. |
| `store_management_action` | Store write/dry-run контракт; проверять только dry-run, apply после revision/idempotency/readback. |
| `store_quote_conductor` | Store quote workflow; шаги могут публиковать предложение. Проверять schema и отдельный Store release gate; live apply — только по точному кейсу. |
| `prepare_action_contract` | Подготавливает guarded action token; без записи бизнес-сущности, но токен привязан к цели/ревизии. Smoke: synthetic fixture в тесте. |
| `lookup_original_parts` | Кандидаты OEM из каталога, read-only provider call; synthetic dry-run/status. Применимость не подтверждается одним источником. |
| `estimate_repair_work_cost` | Оценка работ, read-only; без VIN/клиентских данных в smoke. Не подтверждает цену для клиента. |
| `decode_vehicle_identity`, `decode_vehicle_identities` | VIN/frame identification; read-only/возможен внешний provider call. Smoke: dry-run с синтетическим номером; реальные идентификаторы только в авторизованном кейсе. |
| `catalog_provider_status`, `plan_oem_parts_providers` | Доступность/план OEM провайдеров; read-only, без секретов. Smoke: `catalog_provider_status`; `configured` не равно успешный провайдер. |
| `partsapi_catalog_lookup` | PartsAPI; `dry_run` без provider запроса, live режим читает внешний каталог. Smoke: dry-run и operation status; результат — кандидат, не fitment. |
| `search_partsapi_category_index`, `explain_partsapi_category_for_intent`, `validate_partsapi_category_index` | Исторический индекс категорий старого контракта; read-only fixture. Smoke: synthetic текст и `validate`; не использовать как текущий PartsAPI маршрут. |
| `public_aftermarket_catalog_lookup`, `exist_price_lookup` | Публичные/провайдерские аналоги и цены; read-only внешние запросы, availability и fitment перепроверять. Smoke: schema/dry-run по активным параметрам. |
| `resolve_vin_oem_parts`, `verify_oem_candidates_web` | Многошаговые кандидаты VIN/OEM и публичная проверка; read-only с возможным внешним вызовом. Smoke: synthetic dry-run, без реального VIN. |
| `search_web_multi`, `fetch_page_excerpt`, `fetch_page_browser` | Публичная E9/J1 веб-ветка; сетевое чтение. Smoke: ограниченный поиск без персональных данных, либо `https://example.com/` для static/browser; браузер отдельно требует аттестации. |
| `j1_research_start` | Создаёт временную research job и запускает чтение публичной сети; запись во временный кеш, не CRM. Synthetic smoke только с явно тестовым запросом, затем cancel. |
| `j1_research_status`, `j1_research_results`, `j1_research_document`, `j1_research_report` | Чтение job ID и доказательного отчёта J1; read-only. Тесты с disposable кешем; production — только по текущему job ID. |
| `j1_research_add_queries`, `j1_research_cancel` | Меняет временную J1 job; требует точный job ID, readback статуса. Не выполнять на чужой job как smoke. |
| `assess_part_market`, `benchmark_vin_parts_lookup`, `recommend_automotive_sources`, `lookup_public_automotive_evidence` | Рыночная оценка/benchmark/реестр источников; read-only или публичные provider reads. Smoke: schema или synthetic fixture; не публикует Store quote. |
| `search_offline_parts_catalogs` | Read-only поиск локально извлечённых каталогов; новый 43-й tool в source `4e26c6c`, отсутствовал в production-снимке. Smoke: synthetic артикул, оценить bounded excerpts; находка — кандидат. |
| `parts_store_cards` | Карточки CRM колонки «Магазин автозапчастей»: `list/get` читают, `create/append_note` пишут по exact card/revision/idempotency с Gateway readback. Smoke: schema; без live target не создавать карточку. |

Проверенный production smoke до выпуска: из `/tmp` с `PYTHONPATH=/opt/autostop-manager-releases/current` команда `python -m autostop_manager.cli mcp-probe --url http://127.0.0.1:41931/mcp --provider-failure-check --store-check` дала `ok=true`, 42/42 схемы, synthetic resolver, предсказуемый отказ провайдера и Store capabilities. С `--timeout 90 --browser-check` вызов `fetch_page_browser` по `example.com` также дал `ok=true`. Из source checkout той же машины получен ожидаемый `schema_mismatch` из-за разных ревизий; это не сбой установленного endpoint. После deploy повторить из активной ревизии и требовать 43/43 либо новое опубликованное число.

Диагностика: `transport_route_unavailable` → сокет/порт и unit; `transport_auth_failure` → разрешённый transport; `tool_not_registered` → версия client/endpoint или manifest; `schema_mismatch` → сверить **какой** `PYTHONPATH` импортирован и опубликованный SHA; provider failure → отдельный downstream, не перезапуск MCP. Для Store проверяйте private API отдельно. При неуспехе выпуска используйте только официальный rollback из `deployment_runbook.md`; не переключайте release symlink вручную.

## Полный реестр скриптов этого репозитория

Пути относительно `/opt/AutostopManager`. `RO` — read-only; `SYN` — пишет только disposable/synthetic данные; `LOCAL` — меняет локальное техническое состояние; `EXT` — может менять сервис, сеть, Codex/Telegram или production записи. Показать `--help`/прочитать parser безопаснее, чем пробовать изменяющую команду. Любой installer запускается лишь из разрешённого active immutable release или официальным deploy, с rollback gate.

| Скрипт | Реальный вход и эффект |
| --- | --- |
| `scripts/doctor.sh` | RO wrapper `cli doctor`; без аргументов. |
| `scripts/release-gates.sh` | SYN: локальные doctor/lint/mypy/pytest/coverage в одноразовом каталоге; не deploy. |
| `scripts/install-manager-mcp.sh` | EXT `--activate [--replace-unit]`; ставит/активирует unit только из active release. Без `--activate` всё равно installer, не smoke. |
| `scripts/install-manager-automation.sh` | EXT `--manager-revision SHA [--crm-revision SHA] [--crm-version VERSION] [--activate|--activate-under-hold] [--replace-unit] [--release-attempt-key KEY]`; registry/unit/socket, только через release procedure. |
| `scripts/ensure-automation-group.sh` | LOCAL системная группа/UID-GID для Automation Center; вызывается installer, не readiness. |
| `scripts/backup-manager-automation-state.py` | LOCAL online backup SQLite: `--output PATH [--source PATH]`; выход в root-owned `0700`, readback отдельно. |
| `scripts/install-j1-worker.sh` | EXT `--activate [--replace-unit]`; unit и worker, штатный release. |
| `scripts/install-j1-browser-stack.sh` | EXT `[--activate] [--verify] [--replace-unit]`; Docker images/unit, официальный browser memory/swap gate. |
| `scripts/run-j1-browser-stack.sh` | `revision` RO; `start|stop` EXT контейнеры. Вызывается unit/installer, вручную не запускать для smoke. |
| `scripts/attest-j1-browser-stack.sh` | LOCAL/EXT проверка и release-bound marker после topology probe; не создавать marker вручную. |
| `scripts/install-telegram-bridge.sh` | EXT `--account personal|work --revision <commit>`; подготавливает account release. |
| `scripts/provision-telegram-transcription-model.sh` | EXT `--account work --revision <commit>`; скачивает/готовит voice model. |
| `scripts/deploy_telegram_bridge.sh` | EXT `--account personal|work [--no-start] [revision]`; меняет bridge release/service; официальный порядок с duty pause. |
| `scripts/install-codex-wake.sh` | EXT без аргументов; unit, Codex task/config, backup/rollback. Только active snapshot. |
| `scripts/authorize-telegram-account.sh` | EXT `--account personal|work`; интерактивная авторизация аккаунта, не smoke. |
| `scripts/set-work-telegram-duty.sh` | `--status` RO; `--enable|--disable` EXT меняет входящий рабочий режим. |
| `scripts/run-work-telegram-media.sh` | LOCAL `transcribe|preview --file EXACT_FILE [--language ru] [--delete-after]` или `self-check`; обрабатывает частный временный файл. |
| `scripts/run-work-telegram-monitor-voice.sh` | LOCAL/EXT wrapper `telegram_monitor_voice --event-id ... [--language ru]`; только текущий event и очистка. |
| `scripts/import_offline_parts_catalogs.py` | `--verify-only` RO; иначе LOCAL импорт `--archive ZIP --cache-root PATH` в приватный кеш. Архив — недоверенные данные. |
| `scripts/ocr_offline_parts_catalogs.py` | `--verify-only` RO; иначе LOCAL OCR `--cache-root PATH` с обновлением приватного индекса. |
| `scripts/remove-learning-hooks.py` | RO dry-run по умолчанию; `--apply` LOCAL меняет Codex config с backup. Только если gate сообщил требуемую legacy migration. |

Сервисные unit-файлы и класс запуска: `autostop-manager-mcp.service` (native endpoint, LOCAL service), `autostop-manager-scheduler.service` (job executor, EXT отложенно), `autostop-j1.service` (public network worker, EXT чтение), `autostop-j1-browser.service` (Docker/attestation, EXT), `autostop-work-telegram.service` (рабочий bridge, EXT), `autostop-telegram.service` (личный bridge, EXT), `autostop-codex-wake.service` (Codex turns от event, EXT), `autostop-codex-start.service` (Codex boot daemon, LOCAL). Источник — `deploy/systemd/`; `systemctl is-active` проверяет только процесс, конкретные контракты указаны в [J1](vin_catalog_j1.md), [Telegram и Automation](telegram_automation.md) и [release runbook](../deployment_runbook.md).
