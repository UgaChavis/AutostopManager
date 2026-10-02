# Регулярная локальная копия данных приложений

`scripts/autostop-complete-backup.py` создаёт private bundles в отдельном
`/var/backups/autostop-complete`. Это согласованные точки отдельных компонентов,
а не глобальная транзакция CRM, Store и Manager. Manifest сохраняет интервалы,
Manager revision, CRM OCI revision, Store source revision и container image SHA.
Store source revision сам по себе не доказывает installed source/image parity.

| Live источник | Артефакт и правило |
|---|---|
| Store PostgreSQL | свежий dump канонического `/usr/local/sbin/autostop24-db-backup`, hardlink store.dump на том же filesystem |
| Store files | uploads.tar.gz, photos.tar.gz: SHA всего набора неизменны от начала canonical dump до окончания архивов |
| CRM data | crm-files.tar.gz: state, attachments, shared-files, repair-orders, durable JSON/audit/config data; raw SQLite отдельно |
| CRM SQLite | online snapshots под существующим state.lock, journal_mode=DELETE, полная integrity_check |
| Manager DB | manager.sqlite3: online snapshot, integrity_check |
| Scheduler registry | scheduler.sqlite3: online snapshot, integrity_check |
| M2 журнал | roles.tar.gz: проверенная файловая копия |

Wrapper проверяет root ownership, safe mode и точный SHA256 канонического helper.
Его исходник и retention=7 не меняются. Перед helper под native PG flock фиксируется
набор canonical points; flock отпускается, helper берёт его сам. После completion
wrapper снова берёт flock, выбирает ровно один новый inode/mtime/size point,
проверяет private metadata и pg_restore, затем делает hardlink. Stale, ambiguous
или другой filesystem дают отказ. Canonical points и их содержимое не копируются
и не редактируются wrapper. Store capture идёт первым: последующий отказ CRM
может оставить свежую каноническую DB-копию, сохранив прежние full bundles.

CLI create строго прекращает attempt при failed preflight и не выполняет fallback.
CLI daily сохраняет прежнее nightly DB поведение: если fullbundle failed до helper,
его exact canonical helper выполняется отдельно один раз по прежним health/lock/
retention правилам без нового fullcopy reserve. Если helper уже attempted, повторять
его нельзя. Fullbundlefailure остаётся nonzero; JSON сообщает canonical_attempted/
canonical_succeeded, поэтому G1 видит честную полную ошибку и результат DB части.

До и после всей попытки сравниваются ID, image, StartedAt и mounts контейнеров
CRM/Store/DB, Store source revision, installed Manager revision и helper SHA.
При любом изменении новая full-copy не публикуется. В manifest не сохраняются
environment, runtime tokens, owner identities или customer поля; сам bundle содержит
действующие бизнес/auth-данные и доступен только root.

CRM state.lock совместим с ProcessFileLock/flock. Attachment bytes пишутся до
commit metadata, удаляются после tombstone. Весь файловый набор хешируется до
feed snapshot и после архивации; независимые shared-file/printing writers не
объявляются защищёнными одним state.lock. Изменение файла прерывает attempt,
copied archive bytes также сравниваются с source SHA. Lock budget — 8 секунд,
ожидание — 3 секунды. CRM native writer timeout по текущему source — 10 секунд;
короткая задержка или operator failure всё же возможны. Первый capture требует
тихого окна и проверки длительности. Новый root-owned state.lock не создаётся.

Исключены SQLite WAL/SHM/journals, locks, CRM logs, searxng cache, maintenance
reports и вложенные backup. Telegram sessions, VPN profiles, исходники, ОС,
/etc runtime config, environment secrets и provider snapshots не входят.
Для recovery нужны отдельные source/revision anchors, config и reprovision secrets.

Plan выполняет read-only runtime/helper/mount checks и метаданные объёмов.
Консервативный first-run peak: raw non-PG sources + 64MiB + 130% последнего PG
dump. Резерв — 8GiB. PostgreSQL hardlink занимает 0 дополнительных data bytes;
свежий canonical dump требует временное место до штатного helper retention.
Plan отдельно показывает три retained bundles и steady peak: три прежних + один
staging bundle + свежий PG dump. После bootstrap фактические non-PG artifact bytes
и сохранённый raw baseline позволяют независимо рассчитать measured projection
с поправкой на рост. Оценка не гарантирует максимальный размер: guard проверяет
свободное место при capture и отказывает при исчерпании. Старые owned bundles
не удаляются до новой validated, fsynced и atomic публикации. Retention=3 удаляет
только independently verified private bundles нашего формата; unknown/corrupt/
manual directories сохраняются. Hardlinks сохраняют dump даже после штатного
удаления его canonical directory entry; в затяжном failure периоде это требует
дополнительного capacity readback.

Установка/активация — отдельное поручение M2 после review/tests, разрешённого
manual bootstrap, независимого verify и достаточного steady/peak capacity.
Runner: root750 /usr/local/sbin/autostop-complete-backup, bundle root700/files600.
Drop-in: deploy/systemd/autostop24-db-backup.service.d/complete-data.conf меняет
только ExecStart существующей службы на wrapper daily и задаёт SQLite3.51.3 runtime.
Создать backup directory до daemon-reload. Существующие timer, 03:30UTC trigger
и G1 policy сохраняются; отдельный timer не добавляется. Снятие только этого
drop-in и daemon-reload возвращают исходный canonical helper ExecStart.

Restore выполняется отдельно, сначала в изоляции без сети/отправок. SQLite
integrity и pg_restore в /dev/null не доказывают application recovery. Пишущие
компоненты должны быть остановлены на согласованное quiet window; проверить
exact revision/config и независимо прочитать результат. Не активировать scheduler
и Telegram по старой очереди до сверки idempotency/outbox и внешних receipts.
Offsite destination и шифрование согласуются отдельно; remote writes отсутствуют.
