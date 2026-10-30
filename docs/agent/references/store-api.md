# Адаптер Store

Сценарии: [F1 — адаптер](../modules/F1.md), [F2 — Store](../modules/F2.md). Source: [store_api.py](../../../autostop_manager/store_api.py), [store_integration.py](../../../autostop_manager/store_integration.py), [store_owner_api.py](../../../autostop_manager/store_owner_api.py), [store_quote_conductor.py](../../../autostop_manager/store_quote_conductor.py). Store contract: текущие `/opt/autostopapp/docs/store_agent_api.md`, `docs/openapi/store-agent-v1.json`, `backend/app/routers/agent.py`.

## Доступ и данные

Codex → native Manager MCP (`:41931/mcp`) → adapter → loopback `AUTOSTOP_STORE_API_URL` (`/internal/agent/v1`) → Store PostgreSQL. Owner API использует `/api/v1` и отдельный owner principal. CRM интегрируется через private Store API, не прямую запись в Store DB. Store владеет каталогом, заявками и заказами; Manager хранит только технические cursor/ack и guarded receipts.

Нужны `AUTOSTOP_STORE_API_URL`, `AUTOSTOP_STORE_READ_TOKEN`, `AUTOSTOP_STORE_MANAGE_TOKEN`, `AUTOSTOP_STORE_QUOTE_TOKEN`, `AUTOSTOP_STORE_OWNER_TOKEN`, `AUTOSTOP_MANAGER_DB`; значения не выводи. General reads redacted, full quote read/sourcing требуют scoped token. Точные JSON schemas читай через tools/list, fingerprint — [manifest](../manager_mcp_catalog.json). Entities, detail levels, limits и allowlisted actions определены в store_api.py.

| Инструмент / маршрут | Вход и результат | Эффект |
| --- | --- | --- |
| `store_runtime_status` → `/runtime-status`, `/bootstrap-snapshot` | `live`, `bootstrap_snapshot`; redacted readiness | Default читает локальную готовность/circuit. `live=true` читает HTTP runtime; `bootstrap_snapshot=true` читает HTTP snapshot и имеет приоритет. Checkpoint не меняет. |
| `store_digest` → `/digest` | `baseline`, `since`, `cursor`, `ack_token`, `limit`, `stream`; bounded page | Читает Store по HTTP, пишет Manager baseline/pending/cursor/ack и состояние отказа; первый вызов может создать baseline. Без кейса не продвигай production checkpoint. |
| `store_search` → `/search` | Entity/query/filters/cursor/limit; redacted matches | Чтение с bounded scope. |
| `store_entity_context` → `/entities/{entity}/{id}` | Точные entity/id/detail; один объект | Приватное чтение текущего кейса. |
| `download_store_quote_vin_photo` | Quote ID, expected SHA-256 исходного вложения; bounded JPEG preview | HTTP-чтение; возвращает `data.content_base64`/`content_type` в памяти, локальный файл не создаёт. |
| `get_store_analytics_report` | Период/query; обезличенные агрегаты | HTTP POST читает aggregate-only отчёт; бизнес-запись не выполняет. |
| `store_owner_capabilities` | Query/operation ID; input contract/schema_hash | Холодный/просроченный кеш читает owner-scoped OpenAPI по HTTP; тёплый in-memory cache обходится без сети. Бизнес-данные не возвращает. |
| `store_management_action` → `/actions` | Exact target/revision/changes | Guarded запись: current read → contract → dry-run → fresh key apply → readback. |
| `store_quote_conductor` → owner Admin V2 | `start/status/evidence/draft/publish/reopen/order/handoff/decline`; quote/run/revision | Status читает; остальные шаги могут писать/публиковать. |
| `store_owner_api` → `/api/v1` | Текущий operation_id, typed path/query/body, mode, target/revision/contract/proof | GET читает; prepare проверяет input, dry_run — metadata, apply — запись. High-risk нужен dry-run proof/readback. |

Фото запрашивается только для точного разрешённого quote с scoped quote credential.
`expected_photo_sha256` — SHA-256 исходного вложения из свежего чтения quote; Store использует его как guard текущего фото.
Manager возвращает этот SHA в `summary.attachment.sha256`, а SHA полученных preview-байтов вычисляет отдельно
в `summary.attachment.preview_sha256`. Preview может отличаться от исходного фото; эти хеши не обязаны совпадать.
`data.content_base64` содержит bounded JPEG, `data.content_type` — `image/jpeg`; содержимое остаётся транзитным.
При stale attachment перечитай точный quote. Если вызывающая сторона отдельно сохранила preview в приватный файл,
проверь его по `preview_sha256` и очисти после использования. Telegram download имеет отдельный файловый контракт.

Allowlisted management actions: `assign_quote_request`, `set_quote_request_status`, `update_quote_request_comment`, `set_batch_storage_location`, `mark_order_ready`, `set_order_payment_status`, `add_quote_request_note`. Изменение оплаты требует отдельного текущего поручения владельца, scope `store:write:order_payment` и `owner_intent="owner_finance: <описание поручения>"`; автоматическое предоставление scope не выполняется. При uncertain apply сначала читай exact target/receipt, не повторяй запрос вслепую. HTTP 200/readiness не доказывают запись. Store search и public web не подтверждают применимость детали.

Sourcing — lookup-only: `store_sourcing_offer` не поддерживает entity context.
Ответ с `sourcing_provider_deadline_exceeded`, `sourcing_provider_capacity_exhausted`
или `sourcing_provider_unavailable` сохраняет готовые предложения, но неполон;
пустой ответ не подтверждает отсутствие предложений. Manager принимает эти
фиксированные warning-коды только для sourcing search, а прежний Store без
таких warnings остаётся совместим. Если выпуск Store меняет sourcing timeout или
warning-контракт, проверь совместимость итоговых Manager consumer и Store producer
до открытия приёма задач. Установка выполняется при остановленных приложениях по
[deployment](deployment.md); совместимость промежуточных запущенных версий не нужна.
Текущие лимиты и состояние
выпуска проверяй по установленной ревизии и ответу Store; опубликованные исходники
сами по себе не подтверждают работающий сервер.

## Проверки и восстановление

Native MCP выполняет Store HTTP вне event loop с отдельной ограниченной очередью
чтения и последовательным worker для операций с состоянием; точные бюджеты —
[runtime-контракт](manager-runtime.md#ограничения-выполнения).
При `store_executor_*` смотри `adapter_started`, `adapter_may_continue` и
`meta.outcome_uncertain`: истечение ожидания не останавливает уже начатую операцию.
Для начавшейся записи требуется проверка точного результата до любого повтора.
Перед остановкой native Manager используется отдельный
[Store drain handshake](manager-runtime.md#завершение-store-перед-остановкой-manager).
Он учитывает реальные adapter/HTTP workers после ухода caller; завершённый drain
не заменяет проверку бизнес-результата неопределённой записи.

`StoreApiClient` использует один HTTP deadline на вызов JSON API или загрузки фото:
соединение, заголовки и тело делят `timeout`, а GET-повторы получают только остаток
этого времени. Лимит — до трёх попыток для временных сетевых ошибок и HTTP 5xx;
HTTP 4xx, redirects и повреждённые ответы не повторяются. Ошибки фото 5xx учитываются
в circuit так же, как ошибки JSON API. POST отправляется не более одного раза;
при timeout после начала отправки результат остаётся неопределённым и требует
отдельного чтения. Каждый preread, POST и readback получает собственный deadline.
Известный HTTP-статус ошибки сохраняется и при зависшем или оборванном теле.

Store имеет три отдельных HTTP worker-слота. `store_transport_busy` и
`store_transport_unavailable` до отправки возвращают `request_dispatched=false`,
`outcome_uncertain=false` и не ухудшают circuit. Caller прекращает ожидание по
deadline; слот удерживается до фактического завершения worker, включая очистку и
ожидание системного DNS. Лимиты тела JSON и JPEG проверяются отдельно.

Aggregate-only отчёт `get_store_analytics_report` использует те же HTTP-слоты
своего Store worker и один deadline на соединение, заголовки и тело. Он отправляет
один POST без повторов и redirects, сохраняет HTTP-статус ошибки и отдельный лимит
ответа 256 KiB; служебный drain учитывает очистку его transport worker.

Installed probe запускай из `/tmp` с `PYTHONSAFEPATH=1`, `PYTHONPATH`, равным физическому snapshot после разрешения current; source HEAD проверяется отдельно. Задай `runtime_python` точным проверенным Python из текущего `ExecStart` Manager:

```bash
manager_snapshot=$(readlink -f /opt/autostop-manager-releases/current)
env PYTHONSAFEPATH=1 PYTHONPATH="$manager_snapshot" "${runtime_python:?Set verified service Python}" -m autostop_manager.cli mcp-probe --url http://127.0.0.1:41931/mcp --provider-failure-check --store-check
```

Проверяй initialize/ping, manifest/schemas, `checks.store_runtime_status.ok`, `checks.store_owner_capabilities.ok`, `checks.store_order_search.ok`. Fingerprint mismatch между checkout и installed revision — drift, не автоматически неисправность Store. Write smoke выполняй в synthetic tests с одноразовыми состояниями, без live apply. Release gates: [deployment.md](deployment.md); Store `scripts/run-backend-tests.sh --agent-write-smoke`, затем `--full` проверяют контракт, не качество провайдера.

Диагностика: exact REVISION/image digest → service/container → loopback/private
transport и Nginx public deny → tools/list schema → presence-only token checks →
principal (`401`/`403`) → runtime circuit → Store response contract → business
validation. Provider/auth error не повод для restart.

Общий порядок обслуживания: остановка приложений с сохранением SSH/operator,
одна согласованная копия данных/настроек и одна локальная проверка восстановления,
установка проверенных версий, техническая приёмка и очистка временных материалов.
Business volumes, Manager registry, principal IDs, credentials и история
идемпотентности сохраняются. Совместимый Store уже выбранной версии повторно не
устанавливается. Push в Store `main` запускает `Deploy VPS`, поэтому даже docs-only
merge требует соответствующего окна обслуживания. Полный порядок —
[deployment.md](deployment.md).
