# AUTOSTOP: операционный каталог модулей

Срез исходников: Manager `2064711`, CRM `ac10f534`, Store `1c4734ac` на 2026-09-25. Перед любой операцией перечитайте текущие `AGENTS.md`, активные схемы, `REVISION` и runbook. Срез не доказывает состояние production после этой даты.

| Область | Карточка и источник контракта | Владелец данных |
| --- | --- | --- |
| Codex, Manager CLI, native MCP, Manager tools | [Manager и MCP](manager_codex_mcp.md), `autostop_manager/cli.py`, `mcp_tools.py`, `docs/agent/manager_mcp_catalog.json` | Manager хранит только техническое состояние и минимальную обезличенную память |
| CRM Gateway v2 и CRM MCP | [CRM operations](https://github.com/UgaChavis/AutostopCRM-V1/blob/ac10f5340c21554be8f2bd8f3572b58bbe355ee4/docs/OPERATIONS_RUNBOOK.md), `/opt/autostopcrm/src/minimal_kanban/`, Manager `docs/agent/crm_mcp_catalog.json` | CRM |
| Store Agent/owner API | [Store Agent API](https://github.com/AutoStopKrsk/AutoStop-App/blob/1c4734ac7b9939106b432397267f133ab36b9125/docs/store_agent_api.md), `/opt/autostopapp/backend/app/routers/agent.py`, Store OpenAPI | Store |
| Manager ↔ Store adapter | [Store adapter](store_adapter.md), `autostop_manager/store_api.py`, `store_integration.py`, `store_owner_api.py` | Store; Manager хранит только курсоры и guarded receipts |
| VIN/OEM, PartsAPI, каталоги, J1 | [Автомобильные источники и J1](vin_catalog_j1.md), `docs/partsapi.md`, `docs/offline_parts_catalogs.md` | Manager для технических кандидатов; CRM/Store для кейсов |
| Telegram bridge/wake, Automation Center | [Telegram и автоматизация](telegram_automation.md), `automation_registry.py`, `telegram_wake.py` | Telegram владеет сообщениями; Manager техническим состоянием |
| Службы, контейнеры, CI и release | [Runtime и release](runtime_release.md), [release runbook](../deployment_runbook.md) | Каждый сервис по своей системе |

[Матрица покрытия](coverage_matrix.md) фиксирует проверенные цепочки и пробелы. [Манифест релиза](release_manifest_2026-09-25.md) отделяет опубликованный GitHub от установленного runtime. Имена и схемы Manager MCP сверяются генератором `docs/agent/manager_mcp_catalog.json` и `mcp-probe`; CRM и Store имеют собственные проверки схем. Для полного JSON Schema читайте активный `tools/list`: каталог содержит fingerprint, а не копию каждого поля, которая может устареть.

Операционный порядок: установить точный источник и активную ревизию → проверить transport/endpoint → `tools/list` или CLI `--help` → auth/permission → безопасный read-only вызов → downstream provider → тест и выпускной gate. HTTP 200, зелёный CI и `initialize` по отдельности не подтверждают весь путь. Для изменения записи нужны точная цель, ожидаемая ревизия, dry-run, idempotency и независимое чтение. Для отправки, платежа, покупки и destructive операций нужна отдельная авторизация.
