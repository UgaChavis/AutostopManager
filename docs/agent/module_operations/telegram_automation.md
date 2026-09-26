# Work Telegram bridge/wake и Automation Center

Контракты: [`telegram_bridge.py`](../../../autostop_manager/telegram_bridge.py), [`telegram_wake.py`](../../../autostop_manager/telegram_wake.py), [`automation_control.py`](../../../autostop_manager/automation_control.py), [`automation_daemon.py`](../../../autostop_manager/automation_daemon.py), [`automation_registry.py`](../../../autostop_manager/automation_registry.py), [`automation_timers.py`](../../../autostop_manager/automation_timers.py), текущий [`Telegram skill`](../../../.agents/skills/manage-owner-telegram/SKILL.md) и [`deployment_runbook.md`](../deployment_runbook.md). Контент Telegram и CRM принадлежит их системам; в общей документации только техника. Ревизии и результат выпуска — в [каталоге модулей](README.md); приведённые ниже проверки нужно повторять после релиза.

## Цепь событий и границы

`autostop-work-telegram.service` принимает личное входящее в включённом рабочем режиме → `InboundMonitor` удерживает bounded event только в памяти → `autostop-codex-wake.service` принимает `operation=event` через Unix socket с UID bridge → существующая видимая задача Codex запускает новый ход → агент через bridge/CRM/Store выполняет текущий skill. Wake не опрашивает Telegram и не воспроизводит неизвестный результат. Личный `autostop-telegram.service` — другой аккаунт/релиз. Проверка технического состояния **не читает сообщения**: `monitor-status`, `monitor-events` (только события/счётчики), `telegram_wake status`, `set-work-telegram-duty.sh --status`.

Bridge CLI: `/opt/autostop-work-telegram-venv/bin/python -m autostop_manager.telegram_bridge --account work <command>` при `PYTHONPATH=/opt/autostop-work-telegram-releases/current` и пользователе `autostop-work-telegram`. Схема каждого аргумента — `--account work <command> --help`. Классы: **RO** чтение, **LOCAL** локальное временное состояние, **EXT** Telegram/технический внешний эффект. Для работы с сообщениями нужен точный event target, текущий skill, dry-run → contract token → idempotency key → независимый readback; synthetic smoke в production их не отправляет.

| Команды bridge | Назначение, эффект, безопасная проверка |
| --- | --- |
| `daemon`, `code-login` | Запускают bridge или авторизацию; EXT/служебная запись. Только systemd/штатный setup, не smoke. Credentials/session напрямую не читайте. |
| `probe`, `status`, `owner-status` | RO авторизация и конфигурация. `status` содержит account identity: в технических отчётах выводите лишь flags, не имя/ID. |
| `owner-target`, `owner-configure` | Разрешить текущего владельца (RO) / закрепить числовой ID (запись). Изменение только по явному поручению, затем readback. |
| `notify-owner` | EXT уведомление владельца; dry-run/apply, `--kind`, `--notification-key`, contract token; проверять доставку. Не технический smoke. |
| `monitor-status`, `monitor-events` | RO content-free состояние/очередь. Если `dropped_open_events>0` или сменился `started_at`, проверьте непрерывность; не воспроизводите старые события автоматически. |
| `monitor-read`, `monitor-target`, `monitor-context` | RO текущий event/адресат/контекст; может содержать переписку, для технической диагностики не вызывать. `monitor-target` нужен при обслуживании одного авторизованного события. |
| `monitor-mark` | LOCAL меняет disposition текущего события; только при точном event ID и подтверждённом `no_reply_needed`. |
| `dialogs`, `search`, `resolve-phone`, `read` | RO личных данных/переписки; только по отдельной задаче, не readiness. |
| `send`, `send-photo`, `send-document` | EXT отправка клиенту; dry-run/apply контракт и idempotency, readback. `send-document`/photo дополнительно требуют валидированный файл в outbox. |
| `send-owner-notification`, `owner-notification-readback`, `owner-notification-idempotency-readback` | EXT/raw owner отправка и RO readback по точному message/key; использовать предпочтительно `notify-owner` согласно skill. |
| `download`, `discard-download` | LOCAL приватный временный файл; dry-run/apply download и обязательная очистка/readback. Без текущего event target не выполнять. |

Wake CLI: `python -m autostop_manager.telegram_wake --help` перечисляет `daemon`, `status`, `pause`, `setup`, `probe`. `status` — RO (connected/enabled/queued/failed, без текста); `pause` меняет duty и прерывает ход; `setup` создаёт постоянную задачу/config; `probe` создаёт и архивирует тестовую Codex задачу. Последние три — не ежедневные smoke. `telegram_wake.WakeDispatcher` после RPC rejection или неизвестного исхода выключает приём новых событий; systemd может оставаться `active`. `enabled=false` требует диагностики текущей задачи и события, не простого повторного enable. `wake.json` хранит root-only thread ID; не выводите его в отчёты и не подменяйте другим task.

Безопасные команды для установленного runtime:

```bash
sudo -u autostop-work-telegram env PYTHONPATH=/opt/autostop-work-telegram-releases/current /opt/autostop-work-telegram-venv/bin/python -m autostop_manager.telegram_bridge --account work monitor-status
sudo /opt/AutostopManager/scripts/set-work-telegram-duty.sh --status
env PYTHONPATH=/opt/autostop-work-telegram-releases/current /usr/bin/python3 -m autostop_manager.telegram_wake status
systemctl is-active autostop-work-telegram.service autostop-codex-wake.service
```

Запускайте из `/tmp` или установленного release, не из изменённого checkout; `PYTHONSAFEPATH=1` исключает импорт из текущего каталога и уже задан в systemd. Для точной ревизии bridge проверьте target ссылки `current` (SHA в имени snapshot) и хеши файлов против опубликованного commit; Manager дополнительно имеет `REVISION`. `instruction_sha256` wake подтверждает загруженный prompt, но не весь runtime.

Контрольный RO срез 2026-09-26 до выпуска: Manager/bridge `70571c82e629`, CRM `559101d0b288`; `monitor-status` — `enabled=true`, `open_events=0`, `pending_events=0`, `dropped_open_events=0`; duty — `inbound_enabled`, wake — `enabled=true`, `connected=true`, `queued=0`, `failed=0`. Изолированный `scripts/run-work-telegram-media.sh self-check` реально выполнил VAD и speech inference; `run-work-telegram-monitor-voice.sh --self-check` подтвердил доступность wrapper. Это не доставка клиентского сообщения. В отчётах агрегируйте flags и счётчики, не raw event IDs или account identity.

## Automation Center

`autostop-manager-scheduler.service` (`automation_daemon.AutomationDaemon`) владеет registry `/var/lib/autostop-manager-scheduler/registry.sqlite3` и Unix socket `/run/autostop-manager-automation/control.sock`. Manager MCP объявляет `manager_automations` (`status`, `templates`, `readiness`; RO) и `manager_automation_control` (`preview`, `create_from_template`, `set_enabled`, `set_schedule`, `run_now`, `test_notification`, `archive`). Точная MCP input schema — активный `tools/list`. Локальный CLI `python -m autostop_manager.automation_control` принимает `--payload` JSON, `--idempotency-key`, `--expected-revision`; parser также включает служебный `set_global_hold`, доступный только system actor/release procedure. `AutomationControlService` проверяет peer UID/роль, revision и idempotency; `test_notification` ставит intent в outbox, что может доставить Telegram позже. `run_now` также запускает job позже. Preview не изменяет registry.

| Модуль / операция | Входы, эффект, безопасная проверка |
| --- | --- |
| `AutomationStore` + `crm_digest_v1` | Единственный allowlisted singleton template, по умолчанию OFF; scheduler читает CRM change feed и может уведомить owner через work Telegram. Создание/включение — запись; `templates`/`readiness` — RO. |
| `status`, `templates`, `readiness`, `preview` | RO socket protocol `autostop.manager.automation-control.v1`; `readiness` сообщает `ready`, `checks`, job/timer reconciliation, hold/outbox. Проверка без payload и без чтения CRM записей. |
| `create_from_template`, `set_enabled`, `set_schedule`, `archive` | Запись registry. Нужны idempotency key, точный `job_id`/revision для существующей job, readback статуса; scheduler может позже отправить уведомление. |
| `run_now`, `test_notification` | Запись очереди с внешним отложенным эффектом; только для точной job после owner scope, readback runs/outbox. Не synthetic smoke на production. |
| `set_global_hold` | Только официальный release hold от system actor; меняет глобальное исполнение. Не использовать через обычный MCP. |
| `database_backup`, `app_watchdog` | System timer allowlist с `control_mode=read_only`; Центр показывает drift, не меняет состояние. |
| `managed_pc_health`, `managed_pc_fleet_health`, `managed_pc_pending_cleanup` | System timers с `control_mode=managed`; изменение не входит в обычную диагностику Manager и требует точной ревизии/действующего scope. |

`database_backup` — **копия Store**, не CRM: `autostop24-db-backup.timer` запускает `/usr/local/sbin/autostop24-db-backup`, который читает PostgreSQL контейнер `autostop-db` и сохраняет проверенный `pg_dump` в `/var/backups/autostop24/database`. На MNG1 расписание — ежедневно 03:30 UTC; источник расписания — `systemctl show ... -p TimersCalendar`. G1 показывает состояние, но его переключатель для этой строки заблокирован. Резервное копирование CRM проверяйте отдельно по [runbook CRM](https://github.com/UgaChavis/AutostopCRM-V1/blob/autostopcrm-v1/docs/OPERATIONS_RUNBOOK.md).

`crm_digest_v1` читает change feed CRM, собирает изменения за окно и отправляет краткую сводку владельцу через work bridge. Результат виден в чате владельца; техническое выполнение — в runs/outbox G1. Интервал и часовой пояс берутся из `status.jobs[].schedule`, не из имени задания. В исходном срезе — 20 минут, Asia/Krasnoyarsk, задание OFF; включение разрешает последующие запуски, отключение прекращает их. Не включайте его ради технической проверки. Справка в карте CRM только объясняет это поведение и не вызывает `run_now`.

Безопасная команда из `/tmp`: `env PYTHONPATH=/opt/autostop-manager-releases/current /usr/bin/python3 -m autostop_manager.automation_control readiness`. Ожидаются `ok=true`, `data.ready=true`, `checks` со значениями `ready`, `reconcile_state=in_sync`, отсутствие global hold и blocked outbox. `systemctl is-active autostop-manager-scheduler.service` проверяет только процесс. RO срез 2026-09-26 на `70571c82e629` дал `ready=true`, все пять timer states `in_sync`, hold OFF, blocked outbox 0. Одна `crm_digest_v1` job была OFF/in_sync. Операция `readiness` может вернуть status раньше полного reconciliation; перечитайте состояние до вывода о рассогласовании.

Если `systemctl show` недоступен или истёк его timeout, G1 возвращает `inspection_error`/`system_timer_inspection_failed` для соответствующего таймера, сохраняет последнее желаемое состояние и показывает остальные строки. `ready=false`; unknown не считается выключенным исправным таймером. Проверьте systemd/D-Bus и конкретный unit, восстановите транспорт и перечитайте status. Не меняйте переключатели для устранения ошибки чтения. Синтетический тест в `test_automation_timers.py` покрывает настоящий timeout процесса и отсутствие executable, включая блокировку записи при неизвестном состоянии.

`automation_release` CLI: `hold`, `prepare-held`, `seed`, `adopt-current`, `release-hold`, все требуют один `--release-attempt-key`; это **изменяющие** release операции. `scripts/backup-manager-automation-state.py` делает online backup в root-owned `0700` каталоге; `scripts/install-manager-automation.sh` устанавливает только активный immutable release под hold. Перед изменением: точный SHA, quiescent hold, backup registry/unit/drop-ins/duty; после: scheduler/socket/readiness, пять timers, CRM/Telegram smoke и release hold. При ошибке сохраняйте hold и следуйте [`deployment_runbook.md`](../deployment_runbook.md), не восстанавливайте старую registry поверх новых операций без отдельного решения. В обычном согласованном release эти шаги выполняет `/opt/autostopcrm/deploy.sh`.

Тесты: `tests/test_telegram_bridge.py`, `tests/test_telegram_wake.py`, `tests/test_telegram_automation_control.py`, `tests/test_automation_control.py`, `tests/test_automation_daemon.py`, `tests/test_automation_registry.py`, `tests/test_automation_release.py`, `tests/test_automation_timers.py`, `tests/test_automation_jobs.py`; installers — `tests/test_automation_install.py`, `tests/test_automation_backup.py`. Тесты подтверждают synthetic contracts, но не доставку текущего клиентского сообщения. Разделяйте при отказе systemd, Unix socket, peer permissions, Codex App Server/archived task, schema/contract, Telegram transport и конкретный бизнес workflow. Не перезапускайте bridge/wake только потому, что Codex reply задержался.
