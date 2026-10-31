# Первый переход CRM: проверка границы остановки

Статус: команда остановки и возврата реализована и проверена изолированно;
рабочие службы и приём запросов не менялись.
Installed Manager: `490edc332a3a0eeed626b1860771402170150108`, физический
корень `/opt/autostop-manager-releases/20261030T170100Z-client-490edc332a3a-2608122`.
Рабочая CRM: `fe3c3c73d2800b6381ed2e1a489ec4be742d9e9c`.
Разработка: отдельный worktree `crm-first-transition-20261031` от Manager
`69d54b76d6481b1f8ebc3f3d1d192296ba8c1353`. Проверенные кандидаты выпуска
Manager/CRM не изменяются этой подготовкой.

## Конкретный вариант: переход только из подтверждённого простоя

Не пытаться завершать неизвестные операции принудительно. Допустить переход,
только когда после закрытия внешнего intake краткая остановка всех потоков
точного процесса подтверждает отсутствие принятых HTTP-запросов и работы агента.
В ином случае продолжить старый процесс и отложить переход.

1. По действующему runbook сохранить состояния участников обслуживания,
   закрыть intake и исключить конкурирующий deploy/watchdog/host maintenance.
   Store backup не менять. Прежде любых live-изменений должна быть проверена
   независимая процедура возврата, включая гибель управляющего процесса.
2. Закрепить контейнер, cgroup, PID/start time и inode обоих listener sockets.
   Подтвердить, что в контейнере нет дополнительных writers/потомков.
3. Кратко остановить все потоки точного CRM-процесса; проверить, что остановлены
   все task IDs. Через `pidfd_getfd` перечислить TCP file descriptors самого
   процесса. Подсчёт `/proc/net/tcp` недостаточен: после RST handler продолжает
   удерживать socket в TCP_CLOSE.
4. При наличии принятых API/MCP sockets, изменившейся topology, активного агента,
   pending/running tasks или активного расписания возобновить процесс без gate.
   Agent state читать под его native lock без записи и вывода содержимого.
   Lock брать только неблокирующе: если frozen процесс держит его, сразу CONT
   и отказ до gate, а не ожидание освобождения.
   Board control должен быть выключен; retired followups не создают задачи.
   В точном bootstrap `fe3` автономный agent scheduler не запускается
   (`AgentControlService` создаётся с default `start_scheduler=False`). Все
   прочие producers задач встроенного агента идут через учтённые HTTP handlers;
   host writers проверяются отдельно.
5. Только при выполненных предусловиях закрыть оба kernel listeners через их
   duplicate FD. Это перекрывает и loopback, и ещё не принятый backlog.
   После этого допускается согласованный commit остановки; все ожидаемые
   shutdown callbacks и default executor должны завершиться с exit code 0.
   Прямой OAuth writer заканчивается в MCP eventloop; его успешный thread join
   обязателен. Detached Manager pools этого registrar не пишут в CRM data.
6. Если commit не выполняется, вернуть listener через `listen(original_backlog)`
   **до возобновления процесса**, независимо сверить исходный адрес и порт.
   Это подтверждено только для фиксированных портов данной CRM; повторный
   `bind()` здесь даёт EINVAL. Для synthetic bind(port=0) поведение иное,
   обобщать процедуру нельзя. При смене identity или неизвестном результате
   не повторять операции вслепую.
7. После подтверждённой остановки writers выполнить одну приватную копию и
   изолированное восстановление с точным OAuth-ключом по действующему runbook.
   Это не разрешение устанавливать кандидатов или включать расписание до приёмки.

## Полученные доказательства

- На синтетическом HTTP server с настройками старой CRM проверены client RST,
  обнаружение занятого accepted FD, закрытие intake и сохранение active handler.
- При frozen child проверен backlog: kernel shutdown сбросил непринятое
  подключение. Rebind исходного listener и последующий HTTP-запрос прошли.
- Read-only доступ `pidfd_getfd` к реальному Python-процессу CRM доступен.
  При одном наблюдении найдены по одному listener API/MCP без connected sockets;
  pending/running agent tasks и active schedules отсутствовали. Это наблюдение
  не заменяет повторную атомарную проверку внутри самой процедуры.
- Простое взятие storage locks не является альтернативой: после state commit
  существуют действия над derived files/attachments; текущий host fence
  применяется только после остановки и не переносится на живой процесс.
- На точном образе `fe3` с пустыми synthetic data, без сети и с искусственным
  OAuth-ключом проверены metadata 200, регистрация 201 и encrypted OAuth state.
  Idle freeze/gate/listen-before-CONT вернул API и OAuth endpoints с 200.
  Повторный idle gate/SIGTERM/CONT завершил контейнер с exit 0, без OOM.
- Аудит `fe3` подтвердил: board mutations MCP идут через HTTP, ToolExecutor
  использует default executor, штатный shutdown ждёт его; direct OAuth методы
  синхронно выполняются в MCP eventloop без `await`. Фактический SDK 1.28.1
  вызывает этот provider через await. Ошибки cleanup пробрасываются в main.
- Фактический Manager DB binding находится вне CRM data. Его pool работает
  с Store/technical ledger, а CRM-card executor не включён в данный registrar.
  Незавершённые внешние операции Manager/Store не объявляются завершёнными
  только на основании CRM exit 0 и не воспроизводятся автоматически.
- На настоящем systemd проверена гибель controller через SIGKILL после
  freeze/gate до commit. ExecStopPost запустил recovery в отдельном cgroup:
  проверены PID/start/socket inode/address, выполнены listen, SIGCONT и HTTP 200.
  Временные службы и процесс удалены, результат независимо перечитан.
  Private harness и receipts:
  `/var/lib/autostop-maintenance/crm-bootstrap-recovery-probe-lLjdF9Il/`.
  SHA-256 harness:
  `5c4c221c132623cc0f1a6d8207e43e1400118fb63f176aaaa57ed1282d874d1c`.
- На установленном Docker 29.1.3 отдельно проверено: убийство CLI не отменяет
  начатый `stop --timeout -1`; после pending TERM и CONT контейнер с policy=always
  штатно завершился, RestartCount остался 0. Повтор unknown stop запрещён.
- Новая команда испытана на точном `fe3` image с искусственными данными без сети:
  настоящий systemd controller убит SIGKILL в `stop_running` после pending TERM.
  Независимый worker завершил stop и вернул тот же контейнер с новой healthy
  process generation, RestartCount=0, restore=ok. Копирования/выпуска не было.
  Технический receipt:
  `/var/lib/autostop-maintenance/crm-first-transition-postcommit-5ryrxer7/receipt.json`.

## Разовая команда

Реализация находится в `scripts/crm-first-transition.py`, низкоуровневый gate —
в `scripts/crm_legacy_gate.py`. Применяется только к указанной старой ревизии.

`python3 scripts/crm-first-transition.py plan` читает версию, здоровье и
предусловия без изменения служб. `run` выполняет **проверку остановки и возврата**
через отдельные controller, worker и recovery units. Это не backup и не выпуск;
копирование и установка новой версии остаются следующей отдельной стадией.

До рабочего `run` требуется уже подготовленное окно: внешние consumers и
watchdog остановлены, Store завершён по действующему runbook. Команда только
проверяет эти условия, сама не меняет Store, его backup или дежурства.
Состояние попытки сохраняется отдельно в приватном
`/var/lib/autostop-manager/crm-first-transition`; nightly backup state не меняется.

Worker живёт в отдельном cgroup и отправляет `docker stop --signal SIGTERM
--timeout -1` только точному контейнеру. CONT разрешается лишь после pending TERM
в точном frozen Python. CLI/контроллер не отменяет уже отправленный daemon RPC.
После подтверждённого штатного выхода worker возвращает тот же контейнер с
текущими данными и проверяет здоровье новой process generation. БД назад не
накладывается. Ошибки записи журнала не должны обрывать известную выполняемую
операцию; они сохраняют неуспешный результат проверки.

Неизвестный исход stop/start не повторяется вслепую. При изменении runtime
старые PID/fd не используются. Runtime units из `/run` не являются boot recovery
и не подтверждают восстановление после настоящей перезагрузки.

Итоговая адресная проверка: 24 теста gate/controller/каталога операций прошли,
включая RST, отказ при занятости агента, journal/fsync failure до и после RPC,
отказ от повторения неизвестного stop и возврат по подтверждённому receipt.
Проверка трёх сгенерированных units через `systemd-analyze verify` прошла.
Recovery имеет явный `StartLimitIntervalSec=0`, чтобы ожидание независимого
worker не исчерпало лимит перезапусков службы. После этой unit-property core
image-опыт не повторялся; сама property проверена вместе с unit files.

### Граница применения

Проверены низкоуровневый механизм, точный старый образ и восстановление при
гибели контроллера до и после начала stop на искусственных ресурсах. Private
harness предназначен только для испытаний; рабочая команда находится в Git.
Нельзя переносить synthetic PID/fd/порты или выполнять шаги вручную без recovery.
Перед live-применением требуется актуальный успешный `plan` и подготовленное
окно по installed runbook. Read-only `plan` на сервере показал `ready=false`:
Manager MCP/scheduler и Store ещё работают. Команда их самостоятельно не меняет.

При закрытом listener старый accept loop может загружать CPU или писать ошибки;
ожидать завершения задач в таком состоянии не предполагается. Если busy,
процедура отказывается до gate. Неизвестный исход RPC не считается завершённым;
проверены отказ от повторной отправки и сохранение неопределённости. Остановка
самого worker/daemon, аппаратные сбои и настоящая перезагрузка хоста не
имитировались этой приёмкой.

Ни согласованная копия рабочих данных, ни её восстановление этой подготовкой
не выполнены. Ночное расписание остаётся неустановленным.
