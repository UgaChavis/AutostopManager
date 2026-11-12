# Хосты, службы и резервные копии

MNG1 — прикладной сервер `vps26457.mnogoweb.in`: AutoStop Manager, CRM, Store, Telegram, G1, J1, [сайт автосервиса K1](../modules/K1.md), Nginx и PostgreSQL. Для разработки Manager выбирай актуальный checkout или worktree; `/opt/AutostopManager` также содержит действующие данные и настройки. Source CRM и Store: `/opt/autostopcrm`, `/opt/autostopapp`; отдельный source сайта: `/opt/autostop-website`. Installed Manager: `/opt/autostop-manager-releases/current`; Store: `/opt/autostop-app`; опубликованный сайт: `/var/www/autostop24/current`. Каталоги Git и installed runtime имеют разные роли.

Основной VPN — MNG2 (раньше MNJ2), резервный — `fst.kz`. Перед VPN работой установи точную текущую identity и SSH-доступ; это отдельный scope. Старый alias и `/root/AutostopVPN/repo` на MNG1 не доказывают доступ к MNG2. Для FST используй [VPN skill](../../../.agents/skills/manage-fst-vpn/SKILL.md), проверяя hostname/container и клиентскую связь отдельно. VPN исключён из обычного прикладного выпуска.

## Подключения с MNG1

Снимок проверки от **09.11.2026**: root на MNG1 успешно подключился по SSH через профили `/root/.ssh/config`. Это не гарантия текущей доступности или разрешение на действия. Перед порученной работой проверь адресата, вход и фактические права; не выводи секреты и не копируй приватные ключи в проект.

| Команда на MNG1 | Подтверждённая цель на дату проверки |
| --- | --- |
| `ssh home-pc` | Домашний Windows `DESKTOP-BUSO4I8`, пользователь `codexadmin`; обратный туннель через MNG1, административный токен проверен. |
| `ssh autostop-pc` | Рабочий Windows `DESKTOP-7RAOHT7`, пользователь `9860606`; обратный туннель через MNG1, административный токен проверен. |
| `ssh mng2-from-mng1` | MNG2 `vps28681.mnogoweb.in`, пользователь `root`. |
| `ssh mng3-from-mng1` | MNG3 `vps29817.mnogoweb.in`, пользователь `root`. |

Для MNG2/MNG3 настроены отдельные ключи MNG1 и проверка ключей хостов через `/root/.ssh/known_hosts_autostop_mng`; вход ограничен исходным IP MNG1 на дату проверки. При изменении IP или ошибке проверки ключа выясни причину, не отключая проверку. Доступ к ПК зависит от обратного туннеля; при работе заново проверь hostname, пользователя и, если требуется, административный токен. Для серверов проверь hostname и пользователя. Права на подключение не расширяют само поручение на изменение данных, служб или VPN.

В реестре AutoStop Remote есть ещё три регистрации старого протокола, включая компьютер механиков: на дату снимка они были недоступны, полноценный SSH не подтверждён. Пилот AutoStop Remote 3 на компьютере механика ещё не проводился; регистрации не считать работающими подключениями.

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
| `autostop24.ru`, Nginx site `autostop24-website` | Static release, publish receipt, TLS/redirect/HTTP и публичные маршруты; [порядок сайта](website-runtime.md). Сайт обновляется отдельно от приложений. |
| `autostop-searxng`, optional browser containers | Container health и отдельные static/browser вызовы. Исторический `autostop-crawl4ai` не обязателен текущему runtime; перед старым rollback восстанови точный cold image по CRM runbook. |
| `autostop24-db-backup.timer` | В 03:30 UTC новый Store helper сохраняет одну проверенную генерацию DB, files и settings в `/var/backups/autostop-current/store`; Result и manifest проверяются отдельно. Читаемость dump не заменяет локальное восстановление. |
| `autostop-app-watchdog.timer` | Store deploy сам ставит pause/release; не включай timer вручную внутри deploy. |

Для публичного доступа отдельно сравни DNS A, внешний TCP/TLS/redirect/HTTP, локальные upstream, private endpoint и public deny (`401` private без токена, `404` публичный internal маршрут). Различай DNS/network, transport/protocol, schema, auth/permissions, config, provider и application failures. Логи читай ограниченно, без секретов и business payload.

## Обслуживание хоста и резервирование

Сохранённая точка Manager + CRM и штатная генерация Store описаны в [complete-backup.md](complete-backup.md). Для CRM также предусмотрены отдельная [копия и доставка на согласованный ПК](manager-runtime.md#скрипты-и-службы); фактическое состояние их служб и расписания проверяй перед обслуживанием. Старый complete bundle и `pg-retain` hook не активны.

Хранятся текущие версии приложений и необходимые runtime, без обязательного previous-комплекта: [disk-maintenance.md](disk-maintenance.md). Технические логи — три дня; существующий daily logrotate hook ограничивает download caches и Docker build cache по 1 GiB. Данные CRM/Store, полезная незавершённая работа, инструменты и CI сохраняются. После выпуска сверяй actual paths/images и обновляй policy; старые backup roots не возвращай из legacy-шаблона.

Порядок обновления ОС, выпуска приложений и восстановления — в [инструкции M2](m2-server-update.md).
