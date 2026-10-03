# Обслуживание диска MNG1

Контур — прикладной `vps26457.mnogoweb.in`. Цель принятого владельцем плана: `Available >= 20 000 000 000` байт на `/`. Используется `statvfs.f_bavail`; зарезервированные блоки файловой системы не считаются доступным местом и не перенастраиваются. Перед очисткой и после каждого класса удалений проверяются доступность приложений и фактическое место. Отклонение здоровья останавливает удаления.

## Сохранение данных

Сохраняются работающие версии и одна согласованная полная предыдущая версия, последний проверенный recovery set CRM и Store, три последних календарных дня PostgreSQL по `Asia/Krasnoyarsk` и все dump, связанные с полной локальной копией. Для каждого дня выбирается последняя проверенная точка; полная копия добавляет отдельную защиту. Поэтому три дня могут означать четыре и больше имён. Hardlink не считается дважды при оценке освобождения.

SQLite, uploads/photos, рабочие данные, ключи и VPN, личные сессии, история Codex, Git/stash и неизвестные Docker volumes исключены из удаления. Dirty checkout CI и требуемый Python toolcache также сохраняются. Остановленных контейнеров, volumes, journal и build cache универсальным `prune` обработчик не удаляет. Старые Docker images удаляются по проверенным ID/всем тегам без `--force`, с повторной проверкой ссылок всех контейнеров.

Перед удалением проверяются manifest, SHA256 и чтение сохраняемых архивов: PostgreSQL `pg_restore --list` и полная декомпрессия; tar без извлечения; SQLite read-only integrity/FK. Это проверка носителя, а не доказательство полного восстановления приложения. Последняя полная локальная копия остаётся; ежедневный timer полной копии этим обслуживанием не активируется.

В этой версии полные backup bundles целиком исключены из удаления: их PostgreSQL hardlinks требуют отдельной согласованной ротации. Старые полные копии отмечаются `full_retention_not_enabled`. Очистка удаляет обычные CRM/Store recovery sets и PG-дубликаты; автоматическую полную копию и её retention активируй отдельным проверенным выпуском.

## Исполнение

Runtime устанавливается из проверенного commit в `/usr/local/libexec/autostop-disk-maintenance/releases/<content-SHA256>`; `current` меняется атомарно. Root wrapper `/usr/local/sbin/autostop-disk-maintenance` запускает `/usr/bin/python3 -I`; код не импортируется из рабочего checkout. Policy `/etc/autostop-maintenance/policy.json` имеет root `0600`, родитель `0700`. Receipts и CI recovery state — `/var/lib/autostop-manager/private/disk-maintenance`, root `0700/0600`.

Ручной режим: сначала `autostop-disk-maintenance plan --output /ROOT_PRIVATE/manifest.json`. Затем `autostop-disk-maintenance apply --manifest /ROOT_PRIVATE/manifest.json --approve SHA256_ФАЙЛА`. Manifest связан с policy, хостом, устройством, fingerprint и временем; срок 15 минут. Изменение снимка требует нового плана. Точный список кандидатов повторно проверяется под native lock перед удалением. Нет обхода symlink, границ mount или hardlink. Неизвестные/неполные артефакты сохраняются с причиной.

Блокировки: общий cleanup; полная копия `/var/backups/autostop-complete/.backup.lock`; PostgreSQL `/run/lock/autostop24-db-backup.lock`; CRM deploy `/opt/autostopcrm/.autostop-deploy.lock`; рабочий Telegram `/run/autostop-work-telegram-control.lock`. Busy lock запрещает удаление соответствующих данных. Приложения не останавливаются и не выпускаются.

Для широкого удаления CI временно закрывает admission активных workflows `AutoStopKrsk/AutoStop-App`, сохранив исходное состояние до первой записи. Затем ждёт завершения уже принятых jobs без cancel, повторно проверяет Worker/deploy и останавливает только idle runner. Все исходные состояния восстанавливаются в `finally`, включая недостижение цели или ошибку. Runner запускается перед восстановлением workflows. Исходно отключённые workflows остаются отключёнными; deploy не dispatch-ится. Новая ревизия Store во время паузы отражается в receipt и требует обычного workflow после восстановления admission.

После аварийного завершения `recover-ci` повторяет восстановление только собственных изменений. Recovery timer работает каждые пять минут и после загрузки; общий cleanup lock исключает вмешательство в продолжающийся штатный запуск. Ошибка восстановления остаётся видимой в приватном receipt и журнале, её нельзя объявить успехом очистки.

## Расписание и обновление policy

`autostop-disk-maintenance.timer`: каждый день **04:00 Asia/Krasnoyarsk**, `Persistent=false`. После позднего включения пропущенная ночная очистка сразу не запускается. Длительность удаления ограничена, CI drain имеет отдельный предел 30 минут.

`autostop-disk-maintenance.service` выполняет основной sweep; `autostop-disk-maintenance-recovery.service` возвращает CI, `autostop-disk-maintenance-recovery.timer` повторяет recovery. Эти службы не запускают и не перезапускают Docker или приложения при их недоступности.

Drop-in `autostop24-db-backup.service.d/disk-retention.conf` запускает `pg-retain` после успешной публикации штатного dump. Исходный проверенный PostgreSQL helper и его timer остаются прежними. При занятой блокировке копии не удаляются, результат отмечает `retention_pending`; следующий sweep повторит согласование. Поддерживаемый ручной запуск backup — `systemctl start autostop24-db-backup.service`. Не запускай raw helper сериями: его собственная старая ротация семи файлов не выражает три календарных дня. Pending retention согласуй до следующей ручной копии.

Сохранённый current+previous tuple Manager/Telegram и IDs приложений привязаны к проверенной установке. После нового выпуска изменённый класс возвращает `policy_refresh_required` и сохраняет версии; это не успешная очистка данного класса. Перечитай actual current links, `REVISION`, readiness/model manifests, unit cwd, все container images и recovery manifests. Новый current должен быть независимо проверен, previous должен быть последним полным согласованным комплектом; прежний проверенный current — кандидат previous только при подтверждённой непрерывности выпуска. Сначала подготовь новую private policy и dry-run, затем атомарно замени policy и перечитай. Не определяй rollback одной только датой каталога.

Установка: `python3 scripts/install-autostop-disk-maintenance.py --source-commit ПОЛНЫЙ_ПРОВЕРЕННЫЙ_GIT_SHA --policy-file /ROOT_PRIVATE/policy.json --receipt /НОВЫЙ_ROOT_PRIVATE/install.json`. Исходные policy/wrapper/units сохраняются до activation; failed installation возвращает их. В receipt должны совпасть исходный commit, payload SHA, policy SHA, enabled/active timers; потом проверь `systemctl cat`, `systemd-analyze calendar` и реальный запуск сервиса. Периодические проверки не заменяют проверку свободного места и роста после выпуска.
