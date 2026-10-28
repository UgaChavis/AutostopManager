# Локальный клиентский кандидат, 28.10.2026

Результат этого этапа — локальный кандидат для отдельного код-ревью. Установка,
публикация, реальные сообщения и записи CRM не выполняются.

## Исходное состояние и состав

- Cwd: `/opt/AutostopManager`; HEAD `1823d65a9eb9aaae39dc232ca7b5c9baf2da648e`,
  чужие изменения `telegram_wake.py`/fakes/tests и `.codex-attachments` сохранены.
- Manager: `/root/.codex/worktrees/manager-host-catalogs-20261028`, ветка
  `codex/host-catalogs-20261028`, исходный чистый HEAD
  `c3b49a7e432c856ff514802950f16f61cddaf588`.
- CRM: `/root/.codex/worktrees/crm-manager-catalog-c3b49a-20261028`, ветка
  `codex/manager-catalog-c3b49a-20261028`, исходный чистый HEAD
  `60b4ca39e841269e8c1eb5a4f25e1c8472c9458f`.
- Установленные инструкции прочитаны из физического
  `/opt/autostop-manager-releases/20261027T-M1M2-c7cd1f69`, REVISION
  `c7cd1f69ee5cfe3ae80e1b8bc02f1019b2cc2099`. Это отдельная версия.
- `manager_roles`: оба дежурства OFF, dispatches отсутствуют; состояния, задачи,
  вопросы, approvals и permissions перечитаны. Дежурства не запускались.
  Другие CRM WIP и прежние unknown effects сохранены.

## Checkpoints

1. Границы и исходные SHA проверены; работа разделена по Telegram, автомобилям,
   CRM и клиентским инструкциям, без пересечения редактируемых файлов.
2. Подтверждены ошибки weak VIN hints, Honda canonical field, глубокого JSON,
   Windows exception code и доказательства незапущенного Telegram хода.
3. Добавлено штатное ручное участие, точные указания Codex и all-client routing.
   Неизвестные отправки сохраняют исходные ключи и защиту от повторов. Проверенный ACK
   не становится ни завершением запроса, ни вечным hold; повтор ACK предотвращается.
   Сопоставление учитывает общий native ledger, служебные уведомления и Telethon
   без `from_id`; чужой явный sender отклоняется. Невыясненный native intent того же
   peer блокирует новую отправку и ложное human completion до точной сверки.
4. Клиентский процесс дополнен техническим профилем агрегатов, альтернативными
   источниками, предварительным предложением, медиа и точным видом согласия.
   CRM проверяет сохранённый профиль и принадлежность автомобиля точному клиенту.

## Проверки

Проверяется выбранный checkout, а не одноимённые модули cwd. Использована существующая
`/opt/AutostopManager/.venv`; Telegram dependencies читаются из сохранённого native
Python51752 site-packages. Прикладные окружения и зависимости проекта не менялись.
Для hooks использован отдельный virtualenv cache: общий seed cache был повреждён;
проверки не отключались.
Для subprocess нужны `PYTHONSAFEPATH=1`, `PYTHONDONTWRITEBYTECODE=1` и PYTHONPATH
с candidate и этим site-packages; pytest получает явные candidate config/rootdir.

- Automotive: 489 passed, шесть целевых модулей. Проверены decode → ready → reconcile,
  Skoda/Honda, настоящие конфликты и недоверенный caller JSON, глубокий JSON до
  provider, quota → независимый источник, несколько агрегатов, unknown и partial
  family-подбор без VIN в публичном запросе.
  Модули: `test_identity_reuse_regressions`, `test_caller_ready_identity_trust`,
  `test_automotive_helpers`, `test_vehicle_identity_inputs`,
  `test_vehicle_identity_semantics`, `test_vehicle_identity`.
- Telegram: 841 уникальный сценарий прошёл по этапам, без skips: 560 core и 281
  дополнительных wake cases. Четыре прежних ожидания owner→M1 и два прежних
  ожидания wake исправлены под новый контракт, их адресные повторы прошли.
  Проверены greeting/reaction→содержательный запрос, duplicate, exact peer/lease,
  start rejection/unknown, full/partial manual answer, next client request,
  Codex hints/pause/takeover/resume, high reasoning serialization, schema3→4.
  После доработки общего ledger/TL повторены затронутые native/durable consumers:
  121 passed; ещё три адресных owner-notification/CLI-control cases passed.
  Эти повторы входят в 841 и не суммируются повторно.
- Media: 54 passed: 50 в первом прогоне native/context/fallback и четыре исправленных
  voice-прогона после добавления существующего numpy в окружение subprocess.
  Это не дефект приложения: первый runner не видел numpy. Сценарии проверяют
  точный peer/message/media, readback, повтор без resend, очистку, отказ до intent
  → текст и unknown media → запрет нового эффекта.
  После доработки общего native ledger три fallback-сценария повторены — passed;
  общий счёт не увеличивается.
- CRM Windows: 15 passed, включая mocked PowerShell. Обе формы exception code,
  PID/FILETIME и matching проверены без Windows rehearsal.
- CRM Gateway: три целевых сценария прошли: обычный create/link; profile drift
  → unverified → повтор без второй карточки; exact/foreign/duplicate vehicle links.
- 36-case offline fixture валидна. Четыре дополнительных смысловых наблюдения —
  `tests/fixtures/telegram/client_candidate_observations_20261028.json`.
  Worker traces — предложения, in-memory stubs — отдельная ограниченная проверка;
  они не доказывают native delivery, binary rendering или живую сделку.
- A4/A5 обновлены; project instruction catalog check и `git diff --check` прошли.
- Ruff, commit hooks и `git diff --check` прошли. Локальные коммиты реализации:
  Manager `5c0c7257` (Telegram/медиа) и `8bcf7260` (автомобильные контракты);
  CRM `5e269c27` (Windows) и `d53923e1` (точный readback связей).
  Финальный Manager HEAD включает эту инструкцию/отчёт; CRM-каталог привязывается
  отдельным локальным коммитом к этому точному HEAD, без изменения сервера.

Успешные проверки не повторяются без влияющего изменения или конкретного провала.
Полный CI, нагрузочные испытания, Windows rehearsal и release builds не запускались.

## Передача на отдельное код-ревью

Проверить Telegram schema/control/context/recovery contracts и их потребителей,
VIN trust/reuse и multi-unit fields, CRM readback/link guards, Windows parser,
клиентские инструкции и синхронизацию CRM-каталога с Manager.

Внешний этап: независимое код-ревью; при отдельном поручении — CI и подготовка
выпуска с миграцией/возвратом, актуальными declarations, авторизацией и проверкой
провайдеров. Схема inbox4 несовместима со старым writer; production cutover и rollback
не аттестованы. Live VIN/provider availability, визуальное кадрирование чужих
материалов и реальные CRM/Telegram эффекты этим локальным этапом не подтверждаются.
Store, Instagram, VPN и управляемые ПК вне объёма.

Сохраняемые границы: конфликтный decoder envelope не становится ready identity;
полезный предварительный family-поиск использует известный контекст. Подсказки Codex
хранятся транзитно, а revision/пауза/передача — устойчиво; потерянная после рестарта
подсказка требует повторения или явного resume. Визуальное соответствие детали,
содержимое изображений/документов и техническая применимость остаются предметом
проверки исполнителя по источнику, а не гарантией транспорта.
