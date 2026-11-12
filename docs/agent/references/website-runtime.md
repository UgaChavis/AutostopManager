# Сайт autostop24.ru: исходники, выпуск и проверка

Назначение и связи — [K1](../modules/K1.md), полномочия — [AGENTS.md](../../../AGENTS.md), инженерная работа — [M2](../modules/M2.md). Сайт автосервиса — отдельный статический проект на MNG1. Он обслуживает `autostop24.ru` и `www.autostop24.ru` через выделенный Nginx virtual host.

## Источники и установленная версия

- Source: `/opt/autostop-website`; сначала прочитай его README, профильные документы, HEAD и diff. Сохраняй чужую незавершённую работу.
- Публичные файлы: `public/`; `scripts/publish.py` публикует только точный committed `public/` через `git archive`.
- Releases: `/var/www/autostop24/releases/<release>`; `current` — `/var/www/autostop24/current`.
- Nginx: `/etc/nginx/sites-available/autostop24-website` и соответствующая ссылка в `sites-enabled`; TLS — `/etc/letsencrypt/live/autostop24.ru`.
- Root-only publish receipts и lock: `/var/lib/autostop-maintenance/autostop24-launch-20261104`. Имя каталога историческое; действующий путь определяет выбранный `scripts/publish.py`.

Не угадывай основную ветку или remote: проверь `.git/config`, refs и происхождение согласованного коммита. На 12.11.2026 репозиторий сайта локальный, без remote; `cd737015d5d4c9b64bcd2e76b0f47d4bcd9f5ee1` подтверждён receipt `prepared-direct-20261112-cd737015.json`, а все её 80 файлов совпали с `public/` и published release. Это датированная сверка, не будущий deploy marker. В каждой новой задаче сверь текущую физическую цель, `git_sha` и hashes файлов из `prepared-<release>.json`; наличие release само по себе не означает активацию.

## Изменение и выпуск

1. Установи задачу и утверждённое содержание. Техническое обслуживание не разрешает публиковать новые обещания, цены, рекламу, формы или маркетинговые WIP. Работай в исходниках сайта; Manager содержит навигацию и инструкции K1, а не копию сайта.
2. Проверь затронутую страницу и существующие тесты `tests/test_publish.py` средствами проекта. Сохраняй версионированные assets, требуемые опубликованными страницами и ссылками. Не добавляй зависимости или отдельный runtime ради статических файлов.
3. В разрешённой подготовке из `/opt/autostop-website` выполни `python3 scripts/publish.py prepare --revision <full-sha> --release <unique-name>`. Команда создаёт immutable release и receipt; `current` не переключает. Рабочий `public/` должен быть чистым. До подготовки сверь актуальные CLI и константы выбранного скрипта.
4. В согласованном окне `python3 scripts/publish.py activate <release>` проверяет dedicated DNS/TLS, исходное состояние, меняет только цель сайта и его config, выполняет `nginx -t`, reload и проверки; сохраняет transaction receipt. `preview <release>` меняет живую выдачу на HTTP preview с noindex и отключённым TLS для сайта: это переключение, требующее тех же полномочий.
5. Для разрешённого возврата используй `python3 scripts/publish.py rollback <transaction>` и независимо проверь прежнюю цель/config, HTTP и receipt. Не запускай activate для угадываемого старого release вместо штатного rollback.

Сохраняй текущий release, исходники, receipt и необходимую точку возврата. Перезапуск Manager, CRM, Store, Telegram, Docker или базы данных для обновления статического сайта не нужен. Nginx общий: проверяй выделенный config, остальные virtual hosts и состояние master-процесса по штатному скрипту; глобальный config без отдельного основания не меняй.

## Приёмка

Сверь source SHA с подготовленным receipt и фактическими файлами release; отдельно — physical `current` и successful transaction receipt. Проверь HTTPS 200 на нужных страницах, канонический домен/redirect и сохранение query string, ссылки/локальные assets, `robots.txt`, `sitemap.xml`, сертификат и отсутствие случайного noindex в production. `/mcp`, `/api`, `/.git/config`, внутренние download и неизвестные маршруты должны оставаться закрыты публичному запросу. Обычный HTTP readback не должен выполнять provider/API действия, отправлять Telegram или создавать CRM записи.

Текущая версия сайта использует Яндекс Метрику через общий `/assets/metrika-v1.js`, без Webvisor; приватность и события сверяй с README и кодом сайта. Наличие счётчика или sitemap не доказывает индексирование и настройку рекламного кабинета. Search Console/Вебмастер и рекламные действия проверяй отдельно, только через разрешённый доступ.

## Конструктор Manager

K1 — самостоятельный module с канонической инструкцией `docs/agent/modules/K1.md`. Связи показывают работу Codex/инженера с отдельным проектом сайта, без объявления нового native MCP инструмента. Каталог автомобильных E1–E15 и manifest MCP-инструментов сохраняют собственную область.

При порученном обновлении конструктора прочитай полный свежий owner graph через нативную CRM capability `api:/api/manager_structure`, сохрани schema и числовые типы. Подготовь добавление K1 и обновление source-инструкций по [контракту обновления](m2-server-update.md#комплект-для-выпуска-документации): schema/CAS `expected_version`, стабильные idempotency keys, preview и независимый readback каждой записи. Сохраняй все остальные узлы, canvas, геометрию, родительские связи, manual paths и `tool_statuses`. Не используй E1 migration или полный replace для добавления K1. При конфликте/неизвестном исходе сначала перечитай graph/receipt, затем подготовь следующий шаг от подтверждённой версии.
