# Telegram и центр автоматизаций

Сценарии: [B1 — bridge](../modules/B1.md), [B4 — wake](../modules/B4.md), [G1 — автоматизации](../modules/G1.md), [Telegram skill](../../../.agents/skills/manage-owner-telegram/SKILL.md). Контракты: [telegram_bridge.py](../../../autostop_manager/telegram_bridge.py), [telegram_wake.py](../../../autostop_manager/telegram_wake.py), [automation_control.py](../../../autostop_manager/automation_control.py), [automation_daemon.py](../../../autostop_manager/automation_daemon.py), [automation_registry.py](../../../autostop_manager/automation_registry.py), [automation_timers.py](../../../autostop_manager/automation_timers.py).

## Bridge и wake

При включённом duty work bridge сохраняет входящие в защищённый реестр `/var/lib/autostop-work-telegram/inbound.sqlite3` → отдельный видимый worker Codex получает задание → подтверждает клиентский вход B2 и читает полный клиентский скилл → bridge проверяет действия и явный результат. Все принятые входящие work проходят этот процесс независимо от отправителя, включая владельца. Personal account/session остаются отдельными; после обслуживания personal/work используют общий проверенный Manager source и Python. Содержимое сообщений и медиа в реестр и технические журналы не копируется; история Codex-чата имеет собственное хранение.

Правило задаёт аккаунт-получатель: personal не запускает рабочий worker и автоматические клиентские действия CRM. Owner binding нужен точному адресу `notify-owner` и проверке уведомлений, но не открывает вход управления из переписки. Старые поручения и управляющие фразы в истории рабочего диалога не повышают роль нового сообщения. Обычный клиентский процесс с согласованием, рабочей CRM, readback и передачей сохраняется.

Bridge — единственный писатель SQLite (WAL, synchronous=FULL, каталог 0700, файлы 0600). Ссылка на событие сохраняется после перезапуска и никогда не перепривязывается. Приём подтверждается после commit, независимо от готовности модели. До 10 000 незавершённых событий; завершённые ссылки хранятся семь дней, незавершённые автоматически не удаляются. При переполнении/ошибке хранения сохраняется граница пропуска и состояние ухудшения приёма.

Wake подписывается на изменения реестра через root-only Unix RPC, после подключения перечитывает задания. Уведомления не являются второй очередью. Уже поступившие входящие одного адресата объединяются до пяти сообщений в пятиминутном окне без ожидания по таймеру. Busy означает ожидание завершения хода, а не выключение приёма; перед запуском проверяется свободное состояние точного worker. Неоднозначные сообщения граничной секунды duty сохраняются для ручной сверки: Telegram не передаёт микросекунды, поэтому они не отбрасываются и не запускаются автоматически.

Перед `turn/start` сохраняется намерение, после ответа — точный turn ID. После обрыва известный ход сверяется; неизвестный запуск или эффект остаётся `needs_review` без повтора. Heartbeat и существующее транспортное восстановление относятся к службе, не к агентским целям/таймерам. Не добавляется постоянный опрос Telegram/Store. Восстановление пропущенного ввода выполняется при запуске, переподключении и освобождении ёмкости по сохранённым границам только внутри разрешённых duty-окон; старые тесты и выключенные периоды не воспроизводятся.

Проверенное подтверждение (`kind=ack`) не закрывает событие. Задание завершается явно: `answered`, `awaiting_client`, `handoff`, `no_reply_needed`. Bridge проверяет сохранённые receipts и точные покрытые события; отправка и завершение модели сами по себе не доказывают результат запроса. Существующий idempotency ledger связывается с заданием: неизвестную отправку нельзя обходить сменой ключа. Перед итогом проверяется версия реально обработанного контекста, включая уточнения до dry-run. Голос и другие временные медиа очищаются при успехе и ошибке.

Локальный bridge RPC ограничивает запрос 128 KiB, ответ — 4 MiB. Отдельный лимит ответа учитывает UTF-8 и JSON escaping разрешённой истории до 100 сообщений по 4096 символов; строки ради транспортного лимита не обрезаются. Превышение возвращает существующую ошибку `response_invalid`.

После ожидаемого скачивания голосового события bridge повторно проверяет, что событие открыто, перед сохранением приватного файла. Успешное атомарное переименование завершает запись; удаление временного пути требуется при незавершённой записи. Ошибка удаления staged voice проходит через существующие ограниченные попытки очистки; при постоянном отказе файл и handle сохраняются для точной сверки, очистка не объявляется подтверждённой. Лимит текста расшифровки остаётся 32 Ki символов, а лимит JSON-ответа media runner учитывает их кодирование. Предпросмотр видео использует исходную положительную длительность в расчёте кадров и команде обработки; округление длительности относится только к выдаваемому результату.

Work CLI: пользователь `autostop-work-telegram`, физический snapshot из `/opt/autostop-work-telegram-releases/current` в `PYTHONPATH`, `<runtime-python> -m autostop_manager.telegram_bridge --account work <command>`. Python текущей службы возвращает `bash <snapshot>/scripts/work-telegram-python.sh bridge` (для wake — `wake`): учитываются systemd drop-ins, старый путь venv не предполагается. Snapshot может находиться в Manager или work-Telegram releases. Управляющие job/duty операции запускаются root и не читают credentials. Проверяй `<command> --help`; для installed проверок cwd=/tmp и PYTHONSAFEPATH=1. Сверь effective `ExecStart` и installed `REVISION`; runtime-контракт — [manager-runtime.md](manager-runtime.md).

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
| `job-status`, `job-active`, `job-health` | Состояние заданий и публикация здоровья. |
| `job-owner-control` | Совместимый read-only вызов: после прежних проверок lease/revision/claimed возвращает `handled=false`, не читает сообщение и не исполняет управляющее действие. |
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

Техническая проверка без чтения переписки: installed `set-work-telegram-duty.sh --status`, bridge `status`/`monitor-status` и wake `status`. Авторизация, команды runtime, media/voice, MCP и конкретный клиентский сценарий проверяются отдельно. Синтетические проверки обычного отправителя и владельца должны подтверждать один клиентский маршрут B2; проверки personal и уведомлений владельцу остаются отдельными.

## Automation Center

Управление выполняется по поручению в кабинете или управляющем чате. Входящие рабочего Telegram не вызывают Automation Center как команды владельца; его CLI, UI и штатные управляющие инструменты сохраняются.

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

## Обслуживание и восстановление

Обновление выполняется с полной остановкой приложений по
[deployment.md](deployment.md#обслуживание-с-остановкой-приложений): приём новых задач
закрывается, SSH и управляющая сессия сохраняются. Очереди, выбранный worker,
закреплённые инструкции, personal/work sessions, registry и настройки duty/hold
входят в единственную согласованную копию данных и конфигурации. Проверочное
восстановление выполняется один раз локально и изолированно; отдельная дублирующая
копия automation registry для обычного обновления не нужна.

Перед остановкой сверь активный ход и внешние эффекты. Неизвестный исход сохраняется
для отдельного readback; перезапуск не разрешает повтор отправки. Подтверди остановку
исполнителей до копирования состояния. Существующий hold сохраняет владельца и
attempt до разрешённого снятия: смена причины не передаёт владение.

При запуске проверь installed source/Python, MCP, voice, scheduler/socket/readiness,
managed timers, wake, сохранённую привязку worker и очереди без клиентских отправок.
Приём задач открывается после общей приёмки. Реестр версии 2 сохраняет паузу и
страницы заданий; неподтверждённый durable или RAM-only комплект не запускается
поверх него. Старую registry нельзя класть поверх новых операций.

Установщики `install-telegram-bridge.sh`, `deploy_telegram_bridge.sh`,
`install-codex-wake.sh` и `install-manager-automation.sh` содержат собственные
действия запуска/остановки. Использование требует проверки flags и эффектов
выбранной ревизии; последовательность их вызовов не является обязательным путём
обновления. Уже настроенный worker и его задания сохраняются.

Только если отдельно требуется первый переход с RAM-only, до остановки сохрани
точные открытые ссылки через `scripts/export-work-telegram-inbox.py` в приватный
каталог. `job-import-legacy` сохраняет ID и удерживает импортированные задания для
проверки; импорт не разрешает автоматический replay. Создание нового worker и
такой импорт не входят в обычное обновление.

При ошибке сохрани паузу и данные; диагностика различает systemd, Unix socket/peer
permissions, App Server/archived chat, schema/contract, Telegram transport и
business workflow. Задержка ответа сама не основание перезапускать bridge/wake.
