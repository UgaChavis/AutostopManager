# Обслуживание диска MNG1

Контур — прикладной `vps26457.mnogoweb.in`. Действующая схема после уборки 2026-10-21: сохранять установленные текущие версии, рабочие данные, доступы, полезную незавершённую работу и средства разработки/CI. Предыдущий комплект приложений и несколько календарных дней резервных копий больше не являются обязательным запасом.

## Действующее хранение

- Одна согласованная копия Manager + CRM: `/var/backups/autostop-current/manager-crm/20261021-current`. Одна текущая генерация Store: `/var/backups/autostop-current/store`. Состав, проверка и замена — [complete-backup.md](complete-backup.md).
- `autostop24-db-backup.timer` сохраняет запуск в **03:30 UTC**, но служба использует новый [store-backup.py](../../../scripts/store-backup.py). Прежний `disk-retention.conf`/`pg-retain` после backup и автоматический complete bundle больше не активны. Старые backup roots исключаются из актуальной `/etc/autostop-maintenance/policy.json`; не возвращай их из сохранённого шаблона.
- Технические системные журналы хранятся три дня: journald `MaxRetentionSec=3day`, `MaxFileSec=1day`, существующие лимиты `500M/100M`; logrotate `daily`, `rotate 3`, `maxage 3`; sysstat `HISTORY=3`. Деловые данные, audit и очереди CRM/Store не относятся к техническим логам.
- Ежедневный существующий `logrotate.service` вызывает [technical-retention.py](../../../scripts/technical-retention.py): только закрытые технические логи и разрешённые download caches. Лимиты — 1 GiB суммарно для root/runner pip `http-v2` и npm `_cacache`, ещё 1 GiB для неиспользуемого cache Docker builder `default`. Активные сборки/установки, CI и открытые файлы откладывают очистку. `_npx`, Python/venv, браузеры, модели, образы/volumes и инструменты этой процедурой не удаляются.
- Рабочий Python может находиться в каталоге с историческим именем `cold-releases`: его использование проверяется по effective `ExecStart`, `/proc` и imports. Имя каталога не является основанием для удаления. Текущий путь — [manager-runtime.md](manager-runtime.md).

Существующие `autostop-disk-maintenance.timer` и recovery timer сохраняются с актуальной policy для оставшегося обслуживания runtime/images и возврата собственной паузы CI. Старые backup roots и download caches из их inventory исключены; новую копию данных и новый cache helper этот механизм не удаляет.

Разовая очистка старше суток не задаёт новое постоянное расписание. Ротация оперирует целыми файлами, а native journal vacuum может удалить смешанный сегмент с более свежим хвостом. Не обещай точное сохранение последних 24 часов и не используй такой vacuum для требования сохранить каждый свежий event. Активные текстовые логи не обрезаются. Резервные копии и business state не очищаются по возрасту технических логов.

Свободное место оценивается по `statvfs.f_bavail`; зарезервированные блоки не считаются доступным местом и не перенастраиваются. Перед удалением и после него проверяются текущие версии, доступность приложений, ссылки процессов/mounts и фактическое место. Ошибка здоровья прекращает удаления.

## Исполнение и расписание

Runtime: `/usr/local/libexec/autostop-disk-maintenance/current`, wrapper
`/usr/local/sbin/autostop-disk-maintenance` использует `/usr/bin/python3 -I`.
Policy `/etc/autostop-maintenance/policy.json` — root `0600`, каталог `0700`;
receipts и восстановление CI — `/var/lib/autostop-manager/private/disk-maintenance`.
Проверка policy обязательна перед ручным запуском: старый шаблон не отражает нынешние пути.

`autostop-disk-maintenance.timer` запускается в **04:00 Asia/Krasnoyarsk**,
`Persistent=false`; recovery timer каждые пять минут возвращает только собственную
паузу CI. Обслуживание не останавливает приложения и не перезапускает Docker.

| Служба | Действие |
| --- | --- |
| `autostop-disk-maintenance.service` | Выполняет `maintain`; фактические drop-ins могут добавлять отдельные post-hooks, поэтому запуск всей службы не заменяет ограниченный CLI smoke. |
| `autostop-disk-maintenance-recovery.service` | Возвращает только сохранённые изменения CI своего предыдущего запуска. |

При достаточном месте и подтверждённом пустом плане `maintain` возвращает no-op
под native locks, с повторной проверкой здоровья и снимка; CI не приостанавливается.
Общий cleanup lock, native locks PostgreSQL, CRM deploy и рабочего Telegram
исключают пересечение с копированием и выпуском. Занятость lock означает отсутствие
удалений в этой попытке; отдельно проверь результат `recover-ci` и владельца lock.

Ручной порядок: `autostop-disk-maintenance plan --output /ROOT_PRIVATE/manifest.json`,
затем `apply --manifest /ROOT_PRIVATE/manifest.json --approve SHA256_ФАЙЛА`.
Manifest привязан к policy, хосту, устройству и fingerprints, действует 15 минут.
Изменение снимка требует нового плана. Неизвестные, изменённые, открытые файлы,
symlink/mount переходы и связанные hardlinks сохраняются; универсального prune нет.

Перед широким удалением CI admission закрывается с сохранением исходных состояний.
Принятые jobs завершаются без cancel, останавливается только idle runner.
Восстановление идёт в `finally`: сначала runner, затем workflows; исходно отключённые
остаются отключёнными. `recover-ci` после аварии возвращает только свои изменения;
ошибка остаётся видимой в receipt. Новая версия Store требует обычного workflow,
сам maintenance deploy не запускает.

## Обновление

Установка: `python3 scripts/install-autostop-disk-maintenance.py --source-commit SHA
--policy-file /ROOT_PRIVATE/policy.json --receipt /НОВЫЙ_ROOT_PRIVATE/install.json`.
Прочитай текущую справку и сначала проверь diff policy. Установщик сохраняет прежние
wrapper, policy и units для отката; receipt должен совпасть с commit, payload/policy
SHA и реальными состояниями timers. При `pg_retention_enabled=false` старый
`disk-retention.conf` не устанавливается; изменённый чужой hook нельзя удалять.
Store backup работает по отдельной текущей схеме выше.

Перед timers устанавливается точное root-owned tmpfiles правило для
`/run/autostop-work-telegram-control.lock`, затем проверяется root:root `0600` и
сохранение существующего inode/содержимого. Допускается нормализация прежних
`0640`/`0644` до `0600`; чужое правило, symlink, hardlink или небезопасные права
прерывают установку. Сам runtime lock при откате сохраняется.

После выпуска перечитай actual current links, `REVISION`, manifest, process cwd,
Python и все container images. Обнови pinned policy по независимому readback;
`policy_refresh_required` сохраняет изменённый класс, не подтверждает его очистку.
Cold runtime keeper и coherent current/previous tuples не настроены; PG retention
и старые backup roots исключены. При пустом `coherent_keep_paths` runtime-кандидаты
сохраняются с `policy_refresh_required`. Прежние команды остаются в коде и
Git-истории, но не задают действующий порядок обслуживания.
