# Адаптер Store

Сценарии: [F1 — адаптер](../modules/F1.md), [F2 — Store](../modules/F2.md). Source: [store_api.py](../../../autostop_manager/store_api.py), [store_integration.py](../../../autostop_manager/store_integration.py), [store_owner_api.py](../../../autostop_manager/store_owner_api.py), [store_quote_conductor.py](../../../autostop_manager/store_quote_conductor.py). Store contract: текущие `/opt/autostopapp/docs/store_agent_api.md`, `docs/openapi/store-agent-v1.json`, `backend/app/routers/agent.py`.

## Доступ и данные

Codex → native Manager MCP (`:41931/mcp`) → adapter → loopback `AUTOSTOP_STORE_API_URL` (`/internal/agent/v1`) → Store PostgreSQL. Owner API использует `/api/v1` и отдельный owner principal. CRM интегрируется через private Store API, не прямую запись в Store DB. Store владеет каталогом, заявками и заказами; Manager хранит только технические cursor/ack и guarded receipts.

Нужны `AUTOSTOP_STORE_API_URL`, `AUTOSTOP_STORE_READ_TOKEN`, `AUTOSTOP_STORE_MANAGE_TOKEN`, `AUTOSTOP_STORE_QUOTE_TOKEN`, `AUTOSTOP_STORE_OWNER_TOKEN`, `AUTOSTOP_MANAGER_DB`; значения не выводи. General reads redacted, full quote read/sourcing требуют scoped token. Точные JSON schemas читай через tools/list, fingerprint — [manifest](../manager_mcp_catalog.json). Entities, detail levels, limits и allowlisted actions определены в store_api.py.

| Инструмент / маршрут | Вход и результат | Эффект |
| --- | --- | --- |
| `store_runtime_status` → `/runtime-status`, `/bootstrap-snapshot` | `live`, `bootstrap_snapshot`; redacted readiness | Чтение; сверяй ok/format/circuit. |
| `store_digest` → `/digest` | `baseline`, `since`, `cursor`, `ack_token`, `limit`, `stream`; bounded page | Читает Store, пишет Manager checkpoint; без кейса не продвигай production cursor. |
| `store_search` → `/search` | Entity/query/filters/cursor/limit; redacted matches | Чтение с bounded scope. |
| `store_entity_context` → `/entities/{entity}/{id}` | Точные entity/id/detail; один объект | Приватное чтение текущего кейса. |
| `download_store_quote_vin_photo` | Quote ID, expected SHA-256; bounded JPEG | Приватный временный файл, после сверки очистка. |
| `get_store_analytics_report` | Период/query; обезличенные агрегаты | Чтение по запросу отчёта. |
| `store_owner_capabilities` | Query/operation ID; input contract/schema_hash | Читает права/OpenAPI без бизнес-данных. |
| `store_management_action` → `/actions` | Exact target/revision/changes | Guarded запись: current read → contract → dry-run → fresh key apply → readback. |
| `store_quote_conductor` → owner Admin V2 | `start/status/evidence/draft/publish/reopen/order/handoff/decline`; quote/run/revision | Status читает; остальные шаги могут писать/публиковать. |
| `store_owner_api` → `/api/v1` | Текущий operation_id, typed path/query/body, mode, target/revision/contract/proof | GET читает; prepare проверяет input, dry_run — metadata, apply — запись. High-risk нужен dry-run proof/readback. |

Allowlisted management actions: `assign_quote_request`, `set_quote_request_status`, `update_quote_request_comment`, `set_batch_storage_location`, `mark_order_ready`, `add_quote_request_note`. При uncertain apply сначала читай exact target/receipt, не повторяй запрос вслепую. HTTP 200/readiness не доказывают запись. Store search и public web не подтверждают применимость детали.

## Проверки и восстановление

Installed probe запускай из `/tmp` с `PYTHONSAFEPATH=1`, `PYTHONPATH=/opt/autostop-manager-releases/current`:

```bash
env PYTHONSAFEPATH=1 PYTHONPATH=/opt/autostop-manager-releases/current /opt/AutostopManager/.venv/bin/python -m autostop_manager.cli mcp-probe --url http://127.0.0.1:41931/mcp --provider-failure-check --store-check
```

Проверяй initialize/ping, manifest/schemas, `checks.store_runtime_status.ok`, `checks.store_owner_capabilities.ok`, `checks.store_order_search.ok`. Fingerprint mismatch между checkout и installed revision — drift, не автоматически неисправность Store. Write smoke выполняй в synthetic tests с одноразовыми состояниями, без live apply. Release gates: [deployment.md](deployment.md); Store `scripts/run-backend-tests.sh --agent-write-smoke`, затем `--full` проверяют контракт, не качество провайдера.

Диагностика: exact REVISION/deploy marker → service/container → loopback/private transport и Nginx public deny → tools/list schema → presence-only token checks → principal (`401`/`403`) → runtime circuit → Store response contract → business validation. Provider/auth error не повод для restart. Manager/CRM выпускаются `/opt/autostopcrm/deploy.sh`; Store — GitHub Deploy VPS по своему runbook. Rollback сохраняет business volumes и Manager registry.
