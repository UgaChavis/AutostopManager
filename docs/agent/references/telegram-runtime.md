# Telegram и центр автоматизаций

Сценарии: [B1 — bridge](../modules/B1.md), [B4 — wake](../modules/B4.md), [G1 — автоматизации](../modules/G1.md), [Telegram skill](../../../.agents/skills/manage-owner-telegram/SKILL.md). Контракты: [telegram_bridge.py](../../../autostop_manager/telegram_bridge.py), [telegram_wake.py](../../../autostop_manager/telegram_wake.py), [automation_control.py](../../../autostop_manager/automation_control.py), [automation_daemon.py](../../../autostop_manager/automation_daemon.py), [automation_registry.py](../../../autostop_manager/automation_registry.py), [automation_timers.py](../../../autostop_manager/automation_timers.py).

## Bridge и wake

Work bridge принимает личное входящее при включённом duty → `InboundMonitor` держит bounded event только в памяти → wake принимает `operation=event` через Unix socket с UID bridge → существующий видимый чат Codex получает новый ход → агент выполняет текущий skill через bridge/CRM/Store. Wake не опрашивает Telegram. Неизвестный результат не воспроизводится автоматически. Personal bridge — отдельные account/session/release.

Wake восстанавливает транспорт внутри процесса, сохраняя прежние chat/config и FIFO событий в памяти (до 32 ожидающих событий). Во время `starting`/`reconnecting`/`reconciling` при включённом режиме он принимает события без запуска модели; переполнение отклоняется явно. Очередь не переживает перезапуск самого wake; устаревшие события не заменяются новыми адресатами.

Внутренняя очередь уведомлений хода тоже ограничена 32 записями: только ID и статус ожидаемого или активного хода, без items; ручные и посторонние ходы не накапливаются. Уведомление, пришедшее раньше ответа `turn/start`, сохраняется. Переполнение блокирует обработку с `codex_turn_notifications_full`, сохраняя неизвестный исход без повторного запуска. Сигнал обрыва относится к своему поколению соединения: уже сверенный обрыв не вызывает новую проверку следующего хода. После завершения хода остаточные уведомления удаляются.

При настроенном wake bridge сохраняет открытые ссылки: его общий буфер — 32 события, включая активное и ожидающие. Когда весь буфер открыт, новое событие отклоняется до передачи wake с техническим `work_telegram_monitor_full=true` и счётчиком `rejected_events`; ранее принятые события не вытесняются. После подтверждённого закрытия события его место может занять новое. В режиме монитора без wake сохраняется прежнее поведение ограниченного скользящего буфера.

Heartbeat WebSocket — 20 секунд, таймаут ответа — 20 секунд. После транспортного обрыва повторяются подключение к актуальному сокету, `initialize`, `initialized`, `thread/resume` с `excludeTurns=true`: задержки 1, 2, 4, 8, 16, затем 30 секунд; таймаут подключения и инициализации — 20 секунд. Новое соединение не наследует ответы/уведомления старого. Локальный управляющий сокет и systemd READY доступны независимо от готовности Codex.

Открытый транспорт ещё не разрешает запросы обработки: сверка хода ждёт полного завершения `initialize`/`thread/resume`. Проверка idle перед `turn/start` действительна только для того соединения, которое вернуло её результат; после смены соединения она повторяется. Пауза останавливает проверки метаданных и их таймер, сохраняя ожидание подтверждения прерывания известного хода.

Транспортный обрыв при отправке отказа на неподдерживаемый запрос App Server также запускает восстановление. Повреждённый RPC frame блокирует обработку; ожидание готовности сразу замечает блокировку или паузу даже при сброшенном сигнале, поэтому worker завершает ошибку и освобождает счётчик активности без повторного запуска.

До попытки отправки `turn/start` событие сохраняется для обработки после восстановления. При известном ID начатого хода wake сверяет только его метаданные: `thread/turns/list`, `itemsView=notLoaded`, `sortDirection=desc`, страницы по 20, максимум пять страниц. `completed` завершает ход без повторного запуска; `inProgress` ожидает уведомление и проверку статуса каждые 15 секунд только на время восстановления. Статус хода не заменяет подтверждение отправки в bridge. Потеря ответа запуска без ID, `failed`/`interrupted`, отсутствие точного ID, ошибка схемы или RPC rejection блокируют очередь до ручной сверки; ход не угадывается по последней записи. Обрыв при чтении метаданных снова восстанавливает транспорт. Пауза отменяет восстановление и запрещает новые запуски.

Work CLI: пользователь `autostop-work-telegram`, `PYTHONPATH=/opt/autostop-work-telegram-releases/current`, `/opt/autostop-work-telegram-venv/bin/python -m autostop_manager.telegram_bridge --account work <command>`. Аргументы уточняй через `<command> --help`. В installed проверках запускай из `/tmp` с `PYTHONSAFEPATH=1`; source checkout не должен подменять runtime.

| Команда bridge | Эффект и условия |
| --- | --- |
| `probe`, `status`, `owner-status` | Читают auth/config; в техническом отчёте только flags, без account identity. |
| `monitor-status`, `monitor-events` | Content-free состояние/счётчики. `dropped_open_events>0` или новый `started_at` требуют проверки непрерывности, без автоматического replay. |
| `monitor-read`, `monitor-target`, `monitor-context` | Читают текущий event, адресата и переписку; только авторизованный сценарий, не readiness. |
| `dialogs`, `search`, `resolve-phone`, `read` | Читают личные данные/переписку по поручению. |
| `monitor-mark` | Меняет disposition точного event ID только при установленном `no_reply_needed`. |
| `owner-target`, `owner-configure` | Разрешают текущего владельца / сохраняют его числовой ID. Configure требует поручения и readback. |
| `send`, `send-photo`, `send-document` | Отправляют точному адресату: dry-run, contract token, idempotency, независимый readback; файл photo/document предварительно проверяется в outbox. |
| `notify-owner` | Предпочтительное уведомление владельца: `--kind`, `--notification-key`, dry-run/apply и contract token, затем delivery check. |
| `send-owner-notification`, `owner-notification-readback`, `owner-notification-idempotency-readback` | Raw отправка/readback по точному message/key; обычно используй notify-owner. |
| `download`, `discard-download` | Приватный временный файл: текущий target, dry-run/apply, обязательная очистка и readback. |
| `daemon`, `code-login` | Запуск bridge/авторизации со служебной записью; только systemd/setup, не smoke. Session/credentials напрямую не читай. |

Wake CLI `python -m autostop_manager.telegram_wake --help`: `daemon`, `status`, `pause`, `setup`, `probe`. `status` читает connected/enabled/queued/failed без контента, а также `ready`, `recovery_state`, `reconnect_attempts`, `last_disconnect_at`, `last_disconnect_code`, `outcome_unknown`. Состояния: `starting`, `ready`, `reconnecting`, `reconciling`, `blocked`, `paused`. `connected` показывает транспорт, `ready` — разрешённую обработку после полной инициализации и проверки target. Журнал содержит только технические времена и фиксированные коды; произвольный WebSocket close reason, payload и ID не публикуются. `pause` меняет duty и прерывает ход; `setup` создаёт постоянный чат/config; `probe` создаёт и архивирует тестовый чат. Последние три не подходят для ежедневной диагностики.

После RPC rejection или неразрешённого неизвестного исхода `WakeDispatcher` отключает новые события; systemd может оставаться active. При `enabled=false`/`recovery_state=blocked` проверь текущий чат и event, прежде чем включать duty. Автоматический systemd restart не используется для обхода этой блокировки. Root-only `/etc/autostop-work-telegram/wake.json` хранит thread ID: не публикуй его и не заменяй другим чатом. `instruction_sha256` подтверждает загруженный prompt, не весь runtime. Восстановление метаданных рассчитано на протокол Codex 0.160.0; несовместимая схема не разрешает повтор запуска.

Управление duty: `scripts/set-work-telegram-duty.sh --enable|--disable|--status [--expected-release-dir DIR]`. `--status` читает состояние: отдельно `wake_active` (процесс), `wake_connected`, `wake_ready`, `wake_recovery_state`. `--enable` и `--disable` меняют рабочий режим только по поручению владельца. Повторный `--enable` в восстановлении сохраняет очередь без перезапуска служб; заблокированный wake не сбрасывается автоматически, а неудачное повторное включение сохраняет действующий режим и очередь. После изменения перечитай состояние bridge/wake и проверь непрерывность очереди.

Безопасная техническая проверка, без чтения сообщений:

```bash
sudo -u autostop-work-telegram env PYTHONSAFEPATH=1 PYTHONPATH=/opt/autostop-work-telegram-releases/current /opt/autostop-work-telegram-venv/bin/python -m autostop_manager.telegram_bridge --account work monitor-status
sudo /opt/AutostopManager/scripts/set-work-telegram-duty.sh --status
env PYTHONSAFEPATH=1 PYTHONPATH=/opt/autostop-work-telegram-releases/current /usr/bin/python3 -m autostop_manager.telegram_wake status
systemctl is-active autostop-work-telegram.service autostop-codex-wake.service
```

Сверяй target current snapshot и хеши bridge файлов с опубликованным commit; у Manager дополнительно REVISION. `scripts/run-work-telegram-media.sh self-check` выполняет VAD/inference, monitor-voice wrapper проверяется своим `--self-check`. Эти проверки не доказывают доставку клиенту; отчёт содержит flags/counts, без IDs и account identity.

## Automation Center

Scheduler владеет `/var/lib/autostop-manager-scheduler/registry.sqlite3` и `/run/autostop-manager-automation/control.sock`. `manager_automations` поддерживает `status`, `templates`, `readiness`; `manager_automation_control` — `preview`, `create_from_template`, `set_enabled`, `set_schedule`, `run_now`, `test_notification`, `archive`. Актуальные schemas — tools/list.

Локальный `python -m autostop_manager.automation_control` принимает `--payload` JSON, `--idempotency-key`, `--expected-revision`; socket protocol — `autostop.manager.automation-control.v1`. Проверяются peer UID/роль, revision и idempotency. Preview не меняет registry. Остальные изменения требуют точной job/revision/key и readback; `run_now` и `test_notification` ставят отложенный внешний эффект в runs/outbox. Служебный `set_global_hold` доступен system actor только в release procedure.

- `crm_digest_v1` — единственный allowlisted singleton template, по умолчанию OFF. Он читает CRM change feed за окно и может отправить owner сводку через work Telegram. Расписание/timezone читай из `status.jobs[].schedule`, не из имени или старого снимка. Не включай ради smoke; справка конструктора сама не запускает job.
- `database_backup` и `app_watchdog` — system timers с `control_mode=read_only`: G1 показывает drift, не меняет их.
- `managed_pc_health`, `managed_pc_fleet_health`, `managed_pc_pending_cleanup` имеют `control_mode=managed`; изменение требует своего текущего scope/revision.
- `database_backup` относится к Store: `autostop24-db-backup.timer` → `/usr/local/sbin/autostop24-db-backup` → PostgreSQL `autostop-db` → `/var/backups/autostop24/database`. Текущее расписание проверяй через `TimersCalendar`. CRM backup имеет отдельный [runbook](https://github.com/UgaChavis/AutostopCRM-V1/blob/autostopcrm-v1/docs/OPERATIONS_RUNBOOK.md).

Readiness из `/tmp`:

```bash
env PYTHONSAFEPATH=1 PYTHONPATH=/opt/autostop-manager-releases/current /usr/bin/python3 -m autostop_manager.automation_control readiness
```

Нужны `ok=true`, `data.ready=true`, ready checks, `reconcile_state=in_sync`, отсутствие global hold и blocked outbox; сверяются пять timers. Reconciliation может завершиться позже первого ответа: перечитай status перед выводом о drift. Active scheduler сам не доказывает готовность.

При systemctl timeout/отсутствии executable возвращается `inspection_error`/`system_timer_inspection_failed`; желаемое состояние сохраняется, другие строки доступны, ready=false. Unknown не означает исправный выключенный timer. Исправь systemd/D-Bus/конкретный unit, перечитай состояние; не меняй переключатель ради ошибки чтения.

## Standalone выпуск и восстановление

Согласованный выпуск выполняет [deployment.md](deployment.md); ниже только отдельно порученное standalone восстановление.

Work Telegram: останови duty → `scripts/install-telegram-bridge.sh --account work --revision <commit>` → `scripts/provision-telegram-transcription-model.sh --account work --revision <commit>` → `scripts/deploy_telegram_bridge.sh --account work --no-start <commit>` → `scripts/install-codex-wake.sh` из active snapshot. Не переключай current вручную. До enable проверь CRM/MCP, voice, systemd и версии без клиентских отправок. Personal session сохраняется отдельно.

Automation использует один `--release-attempt-key ATTEMPT` для `hold`, `prepare-held`, `seed`, `adopt-current`, `release-hold`; все меняют состояние:

1. `python -m autostop_manager.automation_release hold --release-attempt-key ATTEMPT`, требуется `quiescent=true`.
2. `scripts/backup-manager-automation-state.py --output NEW_PATH`: online registry backup в root-owned `0700`; отдельно снимки scheduler unit, managed timer drop-ins и Telegram duty.
3. Только active immutable release: `scripts/install-manager-automation.sh --activate-under-hold --release-attempt-key ATTEMPT --manager-revision SHA --crm-revision CRM_SHA [--crm-version CRM_VERSION]`.
4. Scheduler/socket/readiness, пять timers, CRM/Telegram bounded smoke/readback; затем `python -m autostop_manager.automation_release release-hold --release-attempt-key ATTEMPT`.

При ошибке сохраняй hold/work pause и восстанавливай компоненты штатным rollback. Старую registry нельзя класть поверх новых операций без отдельного решения о state recovery. Диагностируй systemd, Unix socket/peer permissions, App Server/archived chat, schema/contract, Telegram transport и business workflow отдельно. Задержка ответа сама не основание перезапускать bridge/wake.
