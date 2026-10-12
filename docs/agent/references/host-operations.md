# Хосты, службы и резервные копии

MNG1 — прикладной сервер `vps26457.mnogoweb.in`: AutoStop Manager, CRM, Store, Telegram, G1, J1, Nginx и PostgreSQL. Source: `/opt/AutostopManager`, `/opt/autostopcrm`, `/opt/autostopapp`. Installed Manager: `/opt/autostop-manager-releases/current`; Store: `/opt/autostop-app`. Каталоги Git и installed runtime имеют разные роли.

Основной VPN — MNG2 (раньше MNJ2), резервный — `fst.kz`; текущий доступ к MNG2 не подтверждён. Перед VPN работой установи точную текущую identity и SSH-доступ; это отдельный scope. Старый alias и `/root/AutostopVPN/repo` на MNG1 не доказывают доступ к MNG2. Для FST используй [VPN skill](../../../.agents/skills/manage-fst-vpn/SKILL.md), проверяя hostname/container и клиентскую связь отдельно. VPN исключён из обычного прикладного выпуска.

## Ревизии и проверки

Manager подтверждается `REVISION`, `MANIFEST.sha256` и cwd активного процесса; CRM — OCI label работающего `autostopcrm`; Store — deploy marker, image и current.env. Git HEAD и зелёный workflow не доказывают установленную ревизию. Проверяй состояние заново, без опоры на датированные снимки.

Разреши Manager current в физический snapshot и читай live-инструкции от него. Для source-работы отдельно запиши checkout/worktree HEAD и dirty diff; смешанная задача сохраняет оба корня. Этот выбор не требует новой runtime-копии или смены cwd — [A3](../modules/A3.md).

| Служба или контейнер | Проверка и граница |
| --- | --- |
| `autostop-manager-mcp.service` | Endpoint `:41931/mcp`, cwd/REVISION, manifest и bounded `mcp-probe`. |
| `autostop-manager-scheduler.service` | Registry `/var/lib/autostop-manager-scheduler/registry.sqlite3`, socket, readiness и пять timer states. |
| `autostop-work-telegram.service`, `autostop-codex-wake.service` | Content-free status, duty, queue и версии; сообщения не читаются readiness. |
| `autostop-telegram.service` | Personal account/session, отдельный scope. |
| `autostop-codex-start.service` | Codex boot daemon и App Server status; restart по разрешённому runbook. |
| `autostop-j1.service`, `autostop-j1-browser.service` | Static probe; для browser ещё renderer/proxy, socket и revision-bound attestation. |
| `autostopcrm`, `autostop-db`, `autostop-app` | Health + revision, private transport и API contract. |
| `autostop-searxng`, optional browser containers | Container health и отдельные static/browser вызовы. Исторический `autostop-crawl4ai` не обязателен текущему runtime; перед старым rollback восстанови точный cold image по CRM runbook. |
| `autostop24-db-backup.timer` | Store PostgreSQL backup: текущий Result и точный `pg_restore --list`; файл сам не доказывает восстановимость. CRM backup проверяется отдельно. |
| `autostop-app-watchdog.timer` | Store deploy сам ставит pause/release; не включай timer вручную внутри deploy. |

Для публичного доступа отдельно сравни DNS A, внешний TCP/TLS/redirect/HTTP, локальные upstream, private endpoint и public deny (`401` private без токена, `404` публичный internal маршрут). Различай DNS/network, transport/protocol, schema, auth/permissions, config, provider и application failures. Логи читай ограниченно, без секретов и business payload.

## Обновление ОС и выпуск

Регулярная локальная копия данных CRM, Manager, scheduler и Store: [complete-backup.md](complete-backup.md). Установка timer требует проверенной новой копии и ёмкости для retention; восстановление и offsite выполняются отдельно.

Освобождение места на MNG1 и ночное сохранение current+rollback/трёх дней PG: [disk-maintenance.md](disk-maintenance.md). Используй private manifest, native locks, cold verification и восстановление CI; после нового выпуска перечитывай pinned policy.

Перед обновлением ОС сохрани root-only пакетный перечень, config служб/сети/proxy, свежий проверенный dump и `/proc/sys/kernel/random/boot_id`. Используй поддерживаемые обновления без снятия vendor phasing и автоматического удаления данных. Reboot — отдельное поручение, после окончания deploy/backup процессов. После него проверь новый boot ID, `uname -r`, SHA, systemd, containers, публичный TLS/HTTP, Gateway/MCP, G1, Telegram duty, J1 static/browser и Codex.

Локальные gates Manager: `scripts/release-gates.sh` с одноразовой БД; CRM: `run_checks.ps1 -Profile ci` и текущий runbook; Store: `scripts/run-backend-tests.sh --agent-write-smoke`, затем `--full`. При failed backup/rollback gate, mandatory CI или неверном target SHA выпуск останавливается. Порядок и восстановление: [deployment.md](deployment.md), текущие `/opt/autostopcrm/docs/OPERATIONS_RUNBOOK.md` и `/opt/autostopapp/docs/deploy_rollback.md`.
