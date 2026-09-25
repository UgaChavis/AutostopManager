# Runtime, backups и выпуск

Срез 2026-09-25: source Manager `/opt/AutostopManager` (Git), CRM `/opt/autostopcrm` (Git), Store `/opt/autostopapp` (Git); installed Manager `/opt/autostop-manager-releases/current` — sealed snapshot с `REVISION`/`MANIFEST.sha256`, Store `/opt/autostop-app` — установленное дерево без Git. Реальную ревизию CRM подтверждает OCI label работающего `autostopcrm`, Store — production deploy marker + image/current.env, Manager — snapshot `REVISION` и cwd активного сервиса. Git HEAD и workflow success отдельно этого не доказывают.

| Unit / контейнер | Назначение и readiness | Зависимость, безопасная проверка |
| --- | --- | --- |
| `autostop-manager-mcp.service` | native MCP; active, endpoint `:41931/mcp` | sealed Manager, Store/CRM downstream; `systemctl show ... -p MainPID -p ActiveState`, `mcp-probe` |
| `autostop-manager-scheduler.service` | Automation Center jobs; readiness + five managed timer states | Manager registry `/var/lib/autostop-manager-scheduler/registry.sqlite3`; `manager_automations(operation=readiness)` |
| `autostop-j1.service` | static public research | SearXNG/Crawl4AI; `python -m autostop_manager.j1_research probe` |
| `autostop-j1-browser.service` | isolated browser bootstrap | renderer/proxy, memory/swap attestation; browser probe только после prerequisites |
| `autostop-work-telegram.service`, `autostop-codex-wake.service` | work bridge → Codex wake | technical monitor/status, без чтения/отправки сообщений |
| `autostop-telegram.service` | personal bridge; отдельный release scope | только status, не смешивать с work session |
| `autostop-codex-start.service` | boot existing Codex daemon | `systemctl status` и App Server status; restart только по runbook |
| `autostopcrm`, `autostop-db` | CRM + PostgreSQL | `docker ps` и OCI revision, CRM read-only endpoint, DB backup timer |
| `autostop-app` | Store API/site | exact image/current.env, `/healthz`, private no-token `401`, public internal `404` |
| `autostop-searxng`, `autostop-crawl4ai`, J1 browser containers | public research backends | container health + static/browser probe, без private case data |
| `autostop24-db-backup.timer` | daily full PostgreSQL dump | `systemctl show ... -p Result`, exact backup `pg_restore --list`; наличие файла одно не доказывает восстановимость |
| `autostop-app-watchdog.timer` | Store deploy watchdog | штатный Store workflow сам ставит pause/release; не включать вручную при deploy |

Проверки из `/opt/AutostopManager` и `/opt/autostopcrm`: `./scripts/release-gates.sh` с disposable DB, CRM `run_checks.ps1 -Profile ci` и gates из его текущего runbook; Store — `scripts/run-backend-tests.sh --agent-write-smoke`, `--full`, GitHub `Deploy VPS`. Для production отдельно сравнить DNS A, внешний TLS/redirect/HTTP, локальные upstream, private endpoint и OCI/REVISION. Логи читать ограниченно и не выводить токены или business payload. Ошибки разделять на DNS/network, transport/protocol, schema drift, auth/permissions, config, provider и application. Stop при failed backup/rollback gate, mandatory CI или неверном target SHA.

Штатный Manager/CRM путь: `scripts/release-gates.sh` → merge актуального `origin/AutostopManager` без rewrite → повтор gates → зелёный PR и merge → exact remote SHA → Store-conductor release gate + hook dry-run → `/opt/autostopcrm/deploy.sh` с `AUTOSTOP_J1_ACTIVATE_ON_DEPLOY=1` для static J1. Orchestrator до maintenance запускает pinned catalog sync, а native Manager MCP теперь активирует по умолчанию; затем сам делает backup, hold, Telegram pause, activation и rollback. Не запускать компонентные installers поверх удачного coordinated deploy. Store `main` push запускает `.github/workflows/deploy-vps.yml` и полный production cutover даже при docs-only diff; workflow проверяет backup/headroom, network/Nginx guards и rollback. Сначала завершите PR/CI и планируйте этот выпуск отдельно от CRM cutover. Подробные команды и остановки: [Manager release runbook](../deployment_runbook.md), `/opt/autostopcrm/docs/OPERATIONS_RUNBOOK.md`, `/opt/autostopapp/docs/deploy_rollback.md`.
