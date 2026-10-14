# Telegram и центр автоматизаций

Сценарии: [B1 — bridge](../modules/B1.md), [B4 — wake](../modules/B4.md), [G1 — автоматизации](../modules/G1.md), [Telegram skill](../../../.agents/skills/manage-owner-telegram/SKILL.md). Контракты: [telegram_bridge.py](../../../autostop_manager/telegram_bridge.py), [telegram_wake.py](../../../autostop_manager/telegram_wake.py), [automation_control.py](../../../autostop_manager/automation_control.py), [automation_daemon.py](../../../autostop_manager/automation_daemon.py), [automation_registry.py](../../../autostop_manager/automation_registry.py), [automation_timers.py](../../../autostop_manager/automation_timers.py).

## Bridge и wake

При включённом duty work bridge сохраняет входящие в защищённый реестр `/var/lib/autostop-work-telegram/inbound.sqlite3` → отдельный видимый worker Codex получает задание → проходит B2 и выбранный скилл → bridge проверяет действия и явный результат. Personal account/session/release остаются отдельными. Содержимое сообщений и медиа в реестр и технические журналы не копируется; история Codex-чата имеет собственное хранение.

Bridge — единственный писатель SQLite (WAL, synchronous=FULL, каталог 0700, файлы 0600). Ссылка на событие сохраняется после перезапуска и никогда не перепривязывается. Приём подтверждается после commit, независимо от готовности модели. До 10 000 незавершённых событий; завершённые ссылки хранятся семь дней, незавершённые автоматически не удаляются. При переполнении/ошибке хранения сохраняется граница пропуска и состояние ухудшения приёма.

Wake подписывается на изменения реестра через root-only Unix RPC, после подключения перечитывает задания. Уведомления не являются второй очередью. Уже поступившие входящие одного адресата объединяются до пяти сообщений в пятиминутном окне без ожидания по таймеру. Busy означает ожидание завершения хода, а не выключение приёма; перед запуском проверяется свободное состояние точного worker. Неоднозначные сообщения граничной секунды duty сохраняются для ручной сверки: Telegram не передаёт микросекунды, поэтому они не отбрасываются и не запускаются автоматически.

Перед `turn/start` сохраняется намерение, после ответа — точный turn ID. После обрыва известный ход сверяется; неизвестный запуск или эффект остаётся `needs_review` без повтора. Heartbeat и существующее транспортное восстановление относятся к службе, не к агентским целям/таймерам. Не добавляется постоянный опрос Telegram/Store. Восстановление пропущенного ввода выполняется при запуске, переподключении и освобождении ёмкости по сохранённым границам только внутри разрешённых duty-окон; старые тесты и выключенные периоды не воспроизводятся.

Проверенное подтверждение (`kind=ack`) не закрывает событие. Задание завершается явно: `answered`, `awaiting_client`, `handoff`, `no_reply_needed`. Bridge проверяет сохранённые receipts и точные покрытые события; отправка и завершение модели сами по себе не доказывают результат запроса. Существующий idempotency ledger связывается с заданием: неизвестную отправку нельзя обходить сменой ключа. Перед итогом проверяется версия реально обработанного контекста, включая уточнения до dry-run. Голос и другие временные медиа очищаются при успехе и ошибке.

Локальный bridge RPC ограничивает запрос 128 KiB, ответ — 4 MiB. Отдельный лимит ответа учитывает UTF-8 и JSON escaping разрешённой истории до 100 сообщений по 4096 символов; строки ради транспортного лимита не обрезаются. Превышение возвращает существующую ошибку `response_invalid`.

После ожидаемого скачивания голосового события bridge повторно проверяет, что событие открыто, перед сохранением приватного файла. Успешное атомарное переименование завершает запись; удаление временного пути требуется при незавершённой записи. Ошибка удаления staged voice проходит через существующие ограниченные попытки очистки; при постоянном отказе файл и handle сохраняются для точной сверки, очистка не объявляется подтверждённой. Лимит текста расшифровки остаётся 32 Ki символов, а лимит JSON-ответа media runner учитывает их кодирование. Предпросмотр видео использует исходную положительную длительность в расчёте кадров и команде обработки; округление длительности относится только к выдаваемому результату.

Work CLI: пользователь `autostop-work-telegram`, `PYTHONPATH=/opt/autostop-work-telegram-releases/current`, `/opt/autostop-work-telegram-venv/bin/python -m autostop_manager.telegram_bridge --account work <command>`. Управляющие job/duty операции запускаются root и не читают credentials. Проверяй `<command> --help`; для installed проверок cwd=/tmp и PYTHONSAFEPATH=1.

| Команда bridge | Назначение |
| --- | --- |
| `status`, `monitor-status`, `monitor-events` | Content-free состояние транспорта, приёма, очереди и ошибок. |
| `owner-status`, `owner-configure` | Проверить привязку владельца; configure меняет её только по поручению и с readback. |
| `dialogs`, `search`, `resolve-phone` | Разрешённый поиск диалога; не заменяет адресата рабочего события. |
| `monitor-target`, `monitor-read`, `monitor-context`, `read` | Точный адресат, роль и разрешённый контекст; context не заменяет исходящие в read. |
| `send`, `send-photo`, `send-document` | Dry-run/apply, исходный ключ, независимый readback; рабочий ответ связан с заданием и версией. |
| `send-voice` | Только personal: приватный outbox OGG/Opus, 10 MiB/10 минут, точный числовой private peer; dry-run/apply и идемпотентность, фактические voice/MIME/duration/size и скачанные байты. |
| `job-read`, `job-refresh`, `job-result`, `job-handoff`, `job-effect-readback` | Прочитать/обновить контекст, завершить задание, передать владельцу или сверить старую отправку без повтора; job/lease/revision не публикуются клиенту. |
| `job-claim`, `job-start-intent`, `job-turn-bound`, `job-subscribe` | Контракт исполнителя: получение, durable запуск, привязка и уведомления. |
| `job-status`, `job-active`, `job-health`, `job-owner-control` | Состояние заданий, публикация здоровья и существующие команды владельца внутри lease. |
| `job-needs-review`, `job-resolve`, `job-release`, `job-import-legacy` | Сверка, ручное разрешение, безопасное освобождение до запуска и точный импорт старых ссылок. |
| `duty-set` | Открыть/закрыть разрешённое окно приёма; обычное управление — duty-script ниже. |
| `owner-target`, `notify-owner` | Точная привязка владельца, dry-run/apply и проверка уведомления. Один ключ на инцидент и один на восстановление; при отключённом транспорте намерение сохраняется. |
| `send-owner-notification`, `owner-notification-readback`, `owner-notification-idempotency-readback` | Raw owner-контракт и точная сверка; обычно используй notify-owner. |
| `download`, `discard-download` | Приватные временные медиа; обязательная очистка и её проверка. |
| `monitor-mark`, `code-login` | Legacy disposition / интерактивная авторизация; не заменяют завершение durable задания. |

Wake CLI: `daemon`, `status`, `pause`, `resume`, `setup`, `select-worker`, `probe`, а также legacy recovery-команды по текущему `--help`. `pause` останавливает исполнение, сохраняя реестр и паузу при перезапуске; `resume` не снимает неопределённые исходы. `select-worker` — отдельно порученное переключение root с ожидаемой конфигурацией и закреплёнными физическим корнем инструкций/REVISION; старый чат и история сохраняются. Setup/probe не применяются как ежедневный readiness-тест.

Ручное завершение после остановившегося хода: поставь исполнителя на `pause`, прочитай точное задание и терминальное состояние его хода, сверь старые эффекты через `job-effect-readback` с исходными ключами. Если эффект подтверждён, `retry_no_effects` запрещён. Для разрешённого ручного ответа используй `job-resolve --decision continue_bound_turn --effects-reviewed`, затем `job-refresh`, актуальный контекст и обычный dry-run/apply → `job-result`; только после проверки результата снимай паузу. Эта команда сохраняет привязку старого хода, нового хода модели не запускает. Неизвестный эффект остаётся удержанным.

Статус различает доступность приёма, соединения и исполнителя, pending/needs_review и возраст старейшего задания. Systemd READY означает готовность локального управления. Логическая остановка может существовать при active-процессе. Wake Restart=on-failure восстанавливает процесс, но не снимает сохранённую проверку; общий Codex daemon ради Telegram автоматически не перезапускается. Повреждённая схема, auth и quota требуют устранения причины, а не бесконечных повторов.

Duty: `scripts/set-work-telegram-duty.sh --enable|--disable|--status [--expected-release-dir DIR]`. Выключение закрывает приём, сохраняет ранее принятые задания и останавливает исполнение; включение открывает новое окно и продолжает разрешённую работу без сброса неизвестности. Поля статуса проверяются независимо; включение приёма не означает, что удержанное задание разрешено повторить.

Техническая проверка без чтения переписки: installed `set-work-telegram-duty.sh --status`, bridge `status`/`monitor-status` и wake `status`. Авторизация, команды runtime, media/voice, MCP и конкретный клиентский сценарий проверяются отдельно. Тест с аккаунта владельца подтверждает только owner-маршрут B2.

## Automation Center

Scheduler владеет `/var/lib/autostop-manager-scheduler/registry.sqlite3` и `/run/autostop-manager-automation/control.sock`. `manager_automations` поддерживает `status`, `templates`, `readiness`; `manager_automation_control` — `preview`, `create_from_template`, `set_enabled`, `set_schedule`, `run_now`, `test_notification`, `archive`. Актуальные schemas — tools/list.

Локальный `python -m autostop_manager.automation_control` принимает `--payload` JSON, `--idempotency-key`, `--expected-revision`; socket protocol — `autostop.manager.automation-control.v1`. Проверяются peer UID/роль, revision и idempotency. Preview не выполняет предлагаемое guarded действие и не меняет desired_state/revision существующего задания. Preview existing job, status и readiness могут инициализировать registry и обновить schema metadata; legacy migration добавляет/заполняет технические поля jobs/runtime/controller. Preview создания из template и templates не обращаются к registry. Остальные изменения требуют точной job/revision/key и readback; `run_now` и `test_notification` ставят отложенный внешний эффект в runs/outbox. Служебный `set_global_hold` доступен system actor только в release procedure.

- `crm_digest_v1` — единственный allowlisted singleton template, по умолчанию OFF. Он читает CRM change feed за окно и может отправить owner сводку через work Telegram. Расписание/timezone читай из `status.jobs[].schedule`, не из имени или старого снимка. Не включай ради smoke; справка конструктора сама не запускает job.
- `database_backup` и `app_watchdog` — system timers с `control_mode=read_only`: G1 показывает drift, не меняет их.
- `managed_pc_health`, `managed_pc_fleet_health`, `managed_pc_pending_cleanup` имеют `control_mode=managed`; изменение требует своего текущего scope/revision.

Активное окно относится к местному времени выбранного timezone. При переводе часов выбирается ближайшее реально существующее окно: повторное открытие не пропускается, полностью отсутствующее окно переносится на следующий подходящий день. Штатная остановка scheduler дожидается текущего run/outbox и сохранения результата перед `stopped`; новые задания после начала остановки не захватываются.

Составные периоды systemd, например `1h 15min`, читаются целиком. Неразобранный фактический период при известном желаемом периоде не доказывает `in_sync`. Ошибка или timeout изменения запускает откат; неуспешный откат возвращает `system_timer_rollback_failed`.

Доказанный отказ bridge `conversation_changed_since_dry_run` происходит до send intent и возвращает digest в `prepared`: повтор сохраняет исходные тело и ключ. Другие неизвестные исходы требуют точного readback без повторной отправки. Ключи уже сохранённых outbox не меняются при обновлении; новые тестовые уведомления получают ключ из своего command ID, сохраняя независимость источников команд.
- `database_backup` относится к Store: `autostop24-db-backup.timer` → `/usr/local/sbin/autostop24-db-backup` → PostgreSQL `autostop-db` → `/var/backups/autostop24/database`. Текущее расписание проверяй через `TimersCalendar`. CRM backup имеет отдельный [runbook](https://github.com/UgaChavis/AutostopCRM-V1/blob/autostopcrm-v1/docs/OPERATIONS_RUNBOOK.md).

Readiness из `/tmp`:

```bash
env PYTHONSAFEPATH=1 PYTHONPATH=/opt/autostop-manager-releases/current /usr/bin/python3 -m autostop_manager.automation_control readiness
```

Нужны `ok=true`, `data.ready=true`, ready checks, `reconcile_state=in_sync`, отсутствие global hold и blocked outbox; сверяются пять timers. Reconciliation может завершиться позже первого ответа: перечитай status перед выводом о drift. Active scheduler сам не доказывает готовность.

При systemctl timeout/отсутствии executable возвращается `inspection_error`/`system_timer_inspection_failed`; желаемое состояние сохраняется, другие строки доступны, ready=false. Unknown не означает исправный выключенный timer. Исправь systemd/D-Bus/конкретный unit, перечитай состояние; не меняй переключатель ради ошибки чтения.

## Standalone выпуск и восстановление

Согласованный выпуск выполняет [deployment.md](deployment.md); ниже только отдельно порученное standalone восстановление.

Первый переход с RAM-only: до остановки сохрани точные ссылки через `python scripts/export-work-telegram-inbox.py --output PRIVATE_NEW_FILE` в root-owned каталоге 0700; helper читает текущий монитор и не публикует идентификаторы. Сверь текущий ход и эффекты, объяви окно переключения. Старый disable теряет RAM-ссылки, поэтому экспорт выполняется первым; переписку целиком не воспроизводи.

Work Telegram: приостанови исполнение и duty → `scripts/install-telegram-bridge.sh --account work --revision <commit>` → provisioning модели → `scripts/deploy_telegram_bridge.sh --account work --no-start <commit>` → `scripts/install-codex-wake.sh` из active snapshot. Новый snapshot содержит REVISION; установка wake оставляет службу неактивной.

Для первого перехода сохрани duty выключенным и новый реестр пустым. Запусти bridge и wake для локального управления, затем поставь wake на паузу и проверь `enabled=false`, отсутствие активного задания и неизвестного хода. Сначала выбери отдельный worker через `select-worker` с ожидаемыми старым thread/config hash и новым физическим root/revision по `--help`. Только затем импортируй сохранённые ссылки через `job-import-legacy --file PRIVATE_FILE --instruction-root PHYSICAL --instruction-revision SHA`: они сохраняют ID и удерживаются для проверки. Перезапусти wake для чтения выбранной конфигурации, проверь сохранность импортированных заданий и узко сверь входящие на границе переключения. Все запуски и перезапуски входят в отдельно согласованный выпуск. При обновлении уже настроенного worker сохраняй его привязку; импортированные и удержанные задания не удаляй ради повторного выбора чата.

До enable проверь MCP, voice, systemd, версии и root/revision без клиентских отправок. Personal session сохраняется отдельно. Current вручную не переключай. Откат не подменяет реестр старой копией: до остановки служб проверяется поддержка его версии целевым комплектом. Реестр версии 2 сохраняет паузу и страницы заданий; неподтверждённый durable или RAM-only комплект не запускается поверх него.

Automation использует один `--release-attempt-key ATTEMPT` для `hold`, `prepare-held`, `seed`, `adopt-current`, `release-hold`; все меняют состояние:

1. `python -m autostop_manager.automation_release hold --release-attempt-key ATTEMPT`, требуется `quiescent=true`.
2. `scripts/backup-manager-automation-state.py --output NEW_PATH`: online registry backup в root-owned `0700`; отдельно снимки scheduler unit, managed timer drop-ins и Telegram duty.
3. Только active immutable release: `scripts/install-manager-automation.sh --activate-under-hold --release-attempt-key ATTEMPT --manager-revision SHA --crm-revision CRM_SHA [--crm-version CRM_VERSION]`.
4. Scheduler/socket/readiness, пять timers, CRM/Telegram bounded smoke/readback; затем `python -m autostop_manager.automation_release release-hold --release-attempt-key ATTEMPT`.

Активный release hold сохраняет владельца и attempt до разрешённого снятия; смена причины не передаёт владение. Ожидание `quiescent=true` сверяет удержание на каждом readback и завершается ошибкой при его потере или замене.

При ошибке сохраняй hold/work pause и восстанавливай компоненты штатным rollback. Старую registry нельзя класть поверх новых операций без отдельного решения о state recovery. Диагностируй systemd, Unix socket/peer permissions, App Server/archived chat, schema/contract, Telegram transport и business workflow отдельно. Задержка ответа сама не основание перезапускать bridge/wake.
