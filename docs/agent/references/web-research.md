# J1: публичное исследование и браузер

Сценарий: [J1](../modules/J1.md). Source: [j1_research.py](../../../autostop_manager/j1_research.py), [j1_fetch.py](../../../autostop_manager/j1_fetch.py), [j1_sources.py](../../../autostop_manager/j1_sources.py), [j1_browser.py](../../../autostop_manager/j1_browser.py), [j1_browser_verify.py](../../../autostop_manager/j1_browser_verify.py). J1 читает публичные материалы, не подтверждает диагноз/OEM/fitment и не владеет CRM/Store.

## Выбор и отчёт

Для обнаружения публичных источников используй встроенный в текущий ход веб-поиск Codex, если он доступен. Передай обезличенный вопрос, возьми адреса конкретных страниц и сохрани происхождение ссылок. Для короткого чтения используй `fetch_page_excerpt`. Для JS-страницы при готовом браузере используй `fetch_page_browser`. Если встроенного поиска нет, `search_web_multi` и `j1_research_start` доступны как маршруты Manager в пределах их обычных ограничений.

Для нескольких найденных страниц `j1_research_sources` ставит ссылки в очередь (`source_urls`, при первом вызове также `objective` и `idempotency_key`). При создании можно передать обезличенный `automotive_context`. Добавление в то же задание принимает `job_id` и `source_urls` и наследует сохранённый контекст. Принимаются до 20 публичных HTTP(S) URL за вызов и до 300 разных URL за задание. Работает каноническая дедупликация. Сетевое чтение выполняет существующий J1 worker после постановки. При известном `job_id` сверяй статус и принятые документы. Если ответ старта потерян вместе с `job_id`, повтори тот же запрос с исходным ключом и тем же порядком ссылок. Ключ действует до истечения срока задания или его удаления при очистке кэша. В этот период точный повтор возвращает исходный ID.

Проверка идёт через `j1_research_status`, `j1_research_results` и `j1_research_document` порциями, затем `j1_research_report` (`autostop.j1.report.v1`). Статус задания по ссылкам показывает `acquisition_mode=sources_only`, `queries_remaining=0`, `pages_remaining` для новых URL и `pages_pending` для принятых материалов, ожидающих завершения. У этого режима пусты план, предложения запросов и ошибки поисковых провайдеров. Отчёт тоже указывает `acquisition_mode=sources_only`. У задания с истёкшим сроком `pages_remaining=0`. Для расширения действующего задания добавляй ссылки через `j1_research_sources`.

В `j1_research_results` пустой `query` перечисляет документы. При непустом запросе `total` считает совпавшие строки, включая фрагменты и целые документы из плоского индекса. Несколько фрагментов могут иметь общий `document_id`. Совпадение в тексте фрагмента сообщает `page`, `fragment_kind`, `section` и `source_url`. Совпадение только в названии указывает на документ целиком. `j1_research_document(page=N)` читает страницу PDF или раздел HTML. Нумерация начинается с 1. `offset` относится к выбранному фрагменту, `previous_page` и `next_page` связывают соседние. `fragment_truncated=true` помечает неполную страницу PDF. Вызов без `page` сохраняет прежнее плоское чтение, в том числе старых документов. В VIN-режиме можно отдельно запустить OCR выбранной страницы сохранённого PDF. Публичный воркер распознаёт пустые страницы смешанного PDF во время загрузки.

По умолчанию `query_mode=lexical` сохраняет полнотекстовый поиск. Публичные режимы `dtc` и `part_number` ищут целый идентификатор. В DTC учитывается суффикс, в артикулах нормализуются пробелы и дефисы. Незавершённая строка обрезанной страницы PDF исключается из точного поиска и цитат отчёта. Совпадение в заголовке помечено `match_kind=title` и не получает ссылку на страницу.

Публичный отчёт считает прочитанные источники и их уровни по всему заданию. Список `sources` сохраняет до 20 источников, включая основания уровня уверенности и по одному примеру подтверждённого цитатой конфликта и исключения уровня A/B, если они есть. При таких ограничениях отчёт дополнительно указывает `qualified_source_scope_limits`. Для более длинного списка появляется ограничение `source_list_truncated`. Цитируемые примеры в отчёте получают URL страницы PDF или раздела HTML, когда такая привязка есть у текста. В `alternative_causes` J1 показывает до трёх разных цитат, отдавая приоритет разным источникам. Ограничения применимости и частоты выводятся для заданий с автомобильным контекстом.

Общий режим по ссылкам отделён от заданий поиска по запросам и VIN. Ранее созданные задания читаются текущими инструментами. `add_queries/cancel` меняют выбранную временную job. VIN-пути также выполняют lifecycle housekeeping, затем требуется status readback.
Порученное исследование одного VIN — самостоятельный `j1_research_vin` в том же MCP, независимо от
E2/E4 и decoder API. Исключение действует только в его job; общий search/fetch/browser и обычный J1
сохраняют запрет VIN. Текущий агент извлекает и записывает цитируемые утверждения через
`j1_research_record_facts`, затем фиксирует анализ. [Подробный контракт](j1-vin-research.md).

Для простой сверки детали ориентир — два разных запроса и две целевые страницы;
следующий вызов должен устранять конкретную неоднозначность. Пустая выдача отличается
от ошибки провайдера, неизвестной разметки и challenge. Challenge не обходится.
Безопасные HTTP/transport причины и upstream `retryable=false` сохраняются до Manager;
старая generic ошибка остаётся с неизвестной причиной, не предполагаемым404.

Elcats/Japancats имеют зафиксированный robots запрет: J1 и общий CRM HTTP/browser
reader блокируют их каталожные страницы до DNS/HTTP. J1 учитывает UTF-8 BOM в robots
и также прекращает чтение запрещённого источника. Не меняй reader/provider/proxy
ради обхода такого отказа. Публичные snippets сохраняют область ссылки; ordinary
robots.txt читается только для проверки политики. Источники и optional local OCR
номерных изображений — [Elcats](elcats.md).

В DDG discovery snippet привязан к принятой ссылке результата. Если следующая ссылка
результата не принята, в том числе из-за лимита 20 результатов, её snippet не изменяет
предыдущую строку. Сохраняются лимит 600 символов и извлечение inline-текста внутри snippet.

Excerpt сообщает известный HTTP-статус, content type, способ извлечения, запрошенный
и эффективный размер, обрезку. Явный PDF-ответ может перейти в существующий bounded
static J1 reader без browser или большой research job. На404 этот путь не запускается.
Текст live HTML/PDF, ранее извлечённый текст и поисковый snippet — разные свидетельства;
snippet и шаблон динамического OE-раздела не подтверждают прочитанный оригинальный номер.
HTML charset читается как параметр `Content-Type`: регистр имени, кавычки и допустимые
quoted-pair экранирования значения не меняют декодирование; текст `charset=...` внутри
другого параметра не подменяет его. При отсутствии charset или неизвестном codec сохраняется UTF-8 fallback.
Обычный HTML `template` и его вложенные шаблоны исключаются из static text/title:
инертный login-dialog не превращает публичную страницу в `requires_human`. Прежнее извлечение
для `shadowrootmode="open|closed"` сохраняется, включая отказ при видимом login; внешний
инертный шаблон скрывает и вложенный DSR. Регистр mode учитывается по HTML, пробелы и неизвестные
значения означают обычный template. В text/html `<template/>` остаётся открытым до `</template>`/EOF;
для `application/xhtml+xml` самозакрывающийся template сохраняет прежнее закрытие по `/>`.
Это локальный учёт контекста, без полной проверки HTML DOM/DSR hosts или CSS; семантика — [HTML Standard](https://html.spec.whatwg.org/multipage/scripting.html#the-template-element).

Общий веб-фасад и прямой J1 reader проверяют URL до передачи адаптеру: приватные адреса,
нестандартные порты, контакты и секреты запрещены. Поисковый запрос с контактом,
секретом или небезопасной встроенной ссылкой отклоняется без отправки и отражения
исходного запроса. Прежнее удаление полного VIN из legacy search и обычные артикулы
сохраняются; приватные данные из возвращённого текста также скрываются.
Конечная точка и многоточие после email считаются пунктуацией: контакт блокируется
во входе и скрывается в выдаче с сохранением этой пунктуации.

Automotive context принимает `make`, `model`, `year`, `engine`, `system`, `symptom`, `dtc`, `part_number`, `market`, `production_date` и `position`. `year` означает модельный год. Полный VIN, контакты и секреты не передаются. Автомобильный профиль строит до пяти начальных запросов и допускает до 12 суммарных запросов и 60 документов. При заполнении лимита документов `queries_skipped` считает поиски, которые воркер пропустил. `queries_partial` считает прерванные поиски, из которых воркер сохранил часть результатов и которые завершились без повтора. Общий профиль допускает до 30 запросов и 300 документов. Кэш хранит до 250 МБ до семи дней. `canonical_url` удаляет tracking. Для автомобильных заданий, заданий по ссылкам и документов с размеченными фрагментами J1 сохраняет различающиеся тексты по разным URL, в том числе близкие версии руководств. Точная копия ссылается на основной документ.

Автомобильный поиск начинает со стандартной выдачи SearXNG и использует запасные маршруты при слабой выдаче. Если первый проход не дал применимого технического источника, J1 может один раз добавить короткий запрос о руководстве по узлу или коду. `automatic_follow_up` в плане отмечает этот запрос. Уточнение расходует исходный бюджет задания и уступает ожидающим запросам пользователя. Применимость уже прочитанных документов проверяется в транзакции чтения, чтобы задание можно было отменить во время проверки.

Стандартный лимит загрузки публичного PDF — 8 МБ. В исследовательском задании воркер принимает до 12 МБ для прямого HTTPS-ответа `200` по адресу `.pdf`, если сервер указывает один точный размер, тип `application/pdf`, а тело имеет сигнатуру PDF. Самостоятельное чтение через `fetch_page_excerpt` использует стандартный лимит. `pdftotext` извлекает до 300 страниц и 250 000 символов при лимите 1 МБ вывода парсера. Ссылка на фрагмент включает `#page=N`. Для смешанного PDF воркер пробует распознать до трёх страниц без текстового слоя, до восьми попыток на задание и до шести секунд на страницу. `fragment_extraction_method` обозначает способ извлечения страницы, `pdf_limitations` перечисляет ограничения. HTML ограничен 2 МБ ответа, 50 000 символов текста и 1000 фрагментов. Ссылка на раздел использует безопасный якорь заголовка, если он есть. При превышении лимита заголовков последний фрагмент содержит оставшийся в пределах текстового лимита материал. У него ссылка на документ целиком и пустое название раздела. Ранее сохранённый плоский текст, HTML без подходящих заголовков и плоский результат резервного извлечения PDF доступны без `page`. Если запрошенного фрагмента нет, возвращается `document_page_unavailable`. Один публичный вызов чтения возвращает до 8000 символов.

Публичное чтение HTML, обычного текста и браузерного результата показывает `body_truncated=true`, когда извлечение было ограничено. Сохранённый текст доступен для чтения. Точный поиск и отчёт используют префикс до последней наблюдаемой границы строки или предложения в обрезанном хвосте. Для PDF `pdf_page_limit_reached` и `pdf_text_limit_reached` сообщают о достижении предела даже на границе завершённой страницы.

Классы источников A — OEM/регулятор/TSB, B — производитель компонента, C — каталог, D — форум или общий контекст. Источники D дают контекст и гипотезы. Оценка частоты требует измеримой совокупности A/B, в остальных случаях возвращается `not_measured`. Отчёт различает область применимости, доступ и пробелы. `applicability_basis` и `applicability_evidence` связывают оценку с цитатами. `exact` требует явной области применимости и совпадения запрошенного идентификатора, если он задан. `confirmed` содержит подходящие источники класса A.

Ключи canonical/discovery сохраняют квадратные скобки IPv6 authority по
[RFC 3986 §3.2.2](https://www.rfc-editor.org/rfc/rfc3986.html#section-3.2.2), чтобы
порт одного адреса не сливался с последним сегментом другого. Прежнее удаление
default-порта (HTTP80, HTTPS443), tracking и fragment сохраняется; недефолтный
допустимый порт и значимые query-параметры, включая пустое значение, остаются.
Это форматирование ключа; допуск URL, DLP и сетевые правила не меняются.

Название неизвестного PDF может помочь поиску, но не подтверждает авторитет источника:
обычный отчёт оставляет такой heuristic в D с `unverified_technical_pdf`. Проверенные
источники A/B сохраняют свой уровень. Независимость источников учитывает canonical URL
и зарегистрированные сайты одного издателя. Завершение обычной job согласовано с принятой отменой
и добавлением запросов; сетевое чтение выполняется вне этой короткой транзакции.
Статус `completed` означает, что очередь обработана. Отчёт показывает число полученных
источников и `search_failures` с причинами и числом неудачных запросов. `search_attempts`
в статусе и отчёте группирует одинаковые исходы последнего поискового прохода по каждому
запросу. Поле хранит маршрут поиска, исход и доступную диагностику движка. `search_no_results`
означает, что записанные маршруты отработали без ошибок и подходящих результатов не дали. Пустая выдача DuckDuckGo должна быть явно распознана. Страница неизвестного формата получает `invalid_response`. `unavailable`
перечисляет уже найденные документы, которые не удалось прочитать.

Обычный `start` создаёт job в `AUTOSTOP_J1_CACHE_DIR`; сеть затем читает worker.
Отдельная VIN-job хранит приватные основания в tmpfs. Status/results/report не ищут и не получают
новые страницы, но открытие обычной SQLite может создать/мигрировать схему. Все VIN-пути перед
доменной проверкой могут удалить истёкшие private jobs, пометить lost-state stubs и восстановить
состояние остановленного worker; cleanup действует также при отключённых VIN-записях.
Поэтому все восемь VIN-aware tools имеют readOnly=false/destructive=true/idempotent=false.
Status/results/report/document/cancel/record_facts работают локально (openWorld=false);
vin/add_queries допускают порученную сеть worker (openWorld=true). General start сохраняет
прежние false/false/false/true: сам start не выполняет worker cleanup.
`ocr=true` ставит дедуплицированную OCR-задачу по cached PDF без новой сети и меняет временную
revision/draft. Scoped receipts/CAS и OCR dedup сохраняются, но не покрывают lifecycle housekeeping.
Jobs не являются долговременной customer memory. Локальный `search_offline_parts_catalogs` — отдельное чтение файлов, не J1: [offline-catalogs.md](offline-catalogs.md).

## Runtime и guards

### Native dispatch статуса J1

- Native status dispatch uses a separate bounded pool with lazily started workers: one worker, one waiting call, fixed 30-second caller wait. It shares no automation, provider-read or web-worker capacity. This serializes only status workers with each other, not all J1 operations; routing, SQLite init and VIN lifecycle housekeeping remain unchanged.
- Executor refusal/failure returns the generic J1 transport envelope `schema=autostop.j1.research.v1`, `ok=false`, `error={code:"j1_status_executor_unavailable",reason:...}`. The finite reasons are `busy`, `wait_timeout`, `execution_failed`, `executor_unavailable`, `executor_draining`; unknown executor reasons map to `executor_unavailable`. No job ID/input is echoed and no routing DB is read to classify an unstarted call. Normal general/VIN responses and domain errors retain their original schemas.
- `stage`, `adapter_started` and `adapter_may_continue` describe dispatch; `outcome_uncertain=true` for any started failed/timed-out operation, including a completed worker exception that may already have changed housekeeping state. It is false only before dispatch. Caller timeout/cancellation does not stop a worker or roll back its effects; the real admission slot remains occupied until completion, while an abandoned queued call does not start. `retryable=false` and `retry_attempted=false`: no automatic retry after an unknown outcome.

### Native dispatch отчёта J1

- `j1_research_report` использует отдельный process-global pool: один worker и один ожидающий вызов, 30 секунд ожидания caller; worker threads запускаются при первом submit. Он не делит capacity со status/read/web/control pools. Status остаётся STATUS ONLY со своим прежним 1+1/30s; это не общая сериализация J1. Routing, SQLite init и VIN housekeeping сохраняются. Вложенный `report.read_only` описывает ledger, а whole-tool hints остаются readOnly=false/destructive=true/idempotent=false/openWorld=false.
- Transport refusal/failure до определения family возвращает `schema=autostop.j1.report.v1`, `ok=false`, object `error.code=j1_report_executor_unavailable` и конечный `error.reason`: `busy`, `wait_timeout`, `execution_failed`, `executor_unavailable`, `executor_draining`. Неизвестная причина маппится в `executor_unavailable`; routing DB не читается, job_id/input не отражается. Нормальные general/VIN payloads и domain errors сохраняют исходные schemas и содержимое.
- `stage`, `adapter_started`, `adapter_may_continue` описывают dispatch; `outcome_uncertain=true` для любого started failed/timed-out вызова, даже после завершившегося исключения, поскольку housekeeping мог изменить state. Только отказ до dispatch имеет false. Caller timeout/cancellation не останавливает worker и не откатывает эффекты: slot занят до реального завершения, abandoned queued call не стартует. `retryable=false`, `retry_attempted=false`; автоматического повтора нет.

### Native dispatch результатов J1

- Только `j1_research_results` использует собственный process-global pool: один worker, один ожидающий вызов, 30 секунд ожидания caller; threads запускаются при submit. Status/report остаются отдельными singleton pools с прежними 1+1/30s. Results не заимствует их или read/web/control capacity; общей сериализации J1 нет. Routing/SQLite init/VIN housekeeping и whole-tool hints readOnly=false/destructive=true/idempotent=false/openWorld=false сохраняются.
- Transport refusal/failure до неизвестной family возвращает `schema=autostop.j1.research.v1`, `ok=false`, object `error.code=j1_results_executor_unavailable`, reason из `busy`, `wait_timeout`, `execution_failed`, `executor_unavailable`, `executor_draining`; неизвестный reason маппится в `executor_unavailable`. Routing DB не читается, job_id/input не отражается. Started failure/timeout имеет `outcome_uncertain=true`, даже после finished exception; до dispatch false. Caller timeout/cancellation не останавливает worker и не откатывает effects; real slot удерживается до completion, abandoned queued call не стартует. Автоматических retry нет: retryable=false/retry_attempted=false.
- Defaults `query=""`, `cursor=0`, `limit=20`, прежние SDK coercions и domain validation сохраняются: limit 1…50, query ≤300 символов с прежними safety/FTS/scope guards. Большие положительные cursor, включая `2**63`/`10**100`, остаются допустимыми и за концом выдачи возвращают пустую страницу/next_cursor=null, без нового cap. General/VIN schemas, routing `vin_store_unavailable` без fallback, ordering/redaction/revisions и normal/domain-error payloads не меняются.

### Native dispatch документов J1

- Только `j1_research_document` использует отдельный process-global pool: один worker, один ожидающий вызов и 30 секунд ожидания caller; threads запускаются при submit. Он не делит capacity со status/report/results или read/web/control pools; прежние три J1 singleton pools остаются 1+1/30s. Общей сериализации J1 нет. Routing, SQLite init, VIN housekeeping и effectful whole-tool hints сохраняются.
- Transport refusal/failure до определения family возвращает `schema=autostop.j1.research.v1`, `ok=false`, object `error.code=j1_document_executor_unavailable`, reason из `busy`, `wait_timeout`, `execution_failed`, `executor_unavailable`, `executor_draining`; неизвестный reason маппится в `executor_unavailable`. Routing DB не читается, job/document/page/input не отражается. Started failure/timeout имеет `outcome_uncertain=true`, даже после finished exception; до dispatch false. Caller timeout/cancellation не останавливает worker и не откатывает effects; slot занят до реального завершения, abandoned queued call не стартует. `retryable=false`, `retry_attempted=false`; автоматических повторов нет.
- Defaults offset=0/max_chars=8000/page=null/ocr=false, прежние SDK coercions offset/max_chars и strict page 1…1000/ocr bool сохраняются. General max_chars 1…8000, VIN 80…50000 и offset≥0 остаются domain contracts; большие offset дают прежний пустой slice/next_offset=null без нового cap. Нормальные general/VIN schemas и errors, snapshot текста/страницы, source lineage/masking, document revisions и match evidence не меняются. OCR только ставит дедуплицированную задачу по cached PDF с прежним budget/revision/queued response; новый pool не запускает OCR вместо worker и не меняет housekeeping.

### Native dispatch уточнения запросов и отмены J1

- `j1_research_add_queries` и `j1_research_cancel` имеют каждый свой process-global pool: один worker, один ожидающий вызов, 30 секунд ожидания caller; threads запускаются при submit. Они не делят capacity друг с другом, status/report/results/document или read/web/control. Это независимое admission, не общая сериализация J1, приоритет отмены или гарантия progress через общие SQLite/file locks.
- Transport refusal/failure до определения family возвращает `schema=autostop.j1.research.v1`, `ok=false`, object `error.code=j1_add_queries_executor_unavailable` или `j1_cancel_executor_unavailable`. Причины: `busy`, `wait_timeout`, `execution_failed`, `executor_unavailable`, `executor_draining`; неизвестная причина маппится в `executor_unavailable`. Routing DB не читается, job/query/input не отражаются. Required arguments, native array shape без нового cap, SDK coercions и normal general/VIN/domain-error payloads сохраняются. Domain query guards остаются прежними: general до30 запросов/500 символов, automotive/VIN cumulative12, DLP/dedup/budget/revision/recipients/expiry и transaction ordering. Whole-tool hints остаются effectful; openWorld=true для add_queries, false для cancel.
- Started failure/timeout имеет `outcome_uncertain=true`, включая finished worker exception. Caller timeout/cancellation не останавливает worker и не откатывает mutation; real slot удерживается до completion, abandoned queued call не стартует, автоматического retry нет. Domain cancel остаётся cooperative request с сохранением active-job slot при inflight; отмена native caller не равна отмене job. Подтверждён source dispatch path, а не измеренный runtime stall или latency/SLO.

### Native dispatch старта, очереди ссылок и записи оснований J1

- `j1_research_start` и `j1_research_sources` делят один process-global pool. У `j1_research_record_facts` отдельный pool. У каждого один worker, один ожидающий вызов и 30 секунд ожидания caller. Потоки создаются при первом вызове. Оба способа старта помещают работу в SQLite. Отдельный сетевой worker забирает её из очереди.
- Tool name заранее определяет transport schema без обращения к DB: start и sources — `autostop.j1.research.v1` / `error.code=j1_start_executor_unavailable` или `j1_sources_executor_unavailable`; facts — `autostop.j1.vin_research.v1` / `j1_record_facts_executor_unavailable`. Причины: `busy`, `wait_timeout`, `execution_failed`, `executor_unavailable`, `executor_draining`; неизвестная причина становится `executor_unavailable`. Job/objective/queries/facts/quotes/VIN/key/revision не отражаются. До admission native validation остаётся прежней; schema/defaults/coercions не меняются. Whole-tool hints: start/sources false/false/false/true, facts false/true/false/false (readOnly/destructive/idempotent/openWorld).
- Start сохраняет objective required, queries=null/max_pages=300/automotive_context=null/profile=general, DLP/URL parsing без DNS, domain limits objective2000, general30 запросов/500 символов/pages1…300 и automotive12/60; native array shape не получает нового cap. Migration commit перед `BEGIN IMMEDIATE`, atomic active-job admission/`j1_busy`, queue commit и normal errors остаются прежними. Facts сохраняет required job/revision/facts/key, StrictInt revision≥0, StrictBool finalize=false, domain100 claims/250000 UTF-8 bytes без нового native cap. Receipt replay идёт до revision CAS; точный historical response может возвращаться после последующих revisions. Privacy/citations/lineage, expiry/capacity, atomic batch rollback и invalidation revision остаются прежними. Финализация требует terminal collection и inflight=false; один cancel_requested этого не доказывает. Non-finalizing записи не получают нового запрета во время collection.
- Started failure/timeout, включая finished exception, имеет `outcome_uncertain=true`. Caller timeout/cancellation не останавливает worker, не доказывает rollback и не освобождает slot до реального completion; abandoned queued call не стартует. Native caller cancellation не отменяет job. Retry автоматически не выполняется. У `j1_research_start` нет ключа идемпотентности: при неизвестном исходе сверяй точное задание. Для `j1_research_sources` проверяй статус по известному ID. При потере ID точный повтор старта с прежним телом и ключом восстанавливает ID в пределах срока хранения задания. Для facts до допустимого повтора нужно сверить точный receipt/состояние; использовать исходные job/key/body, не новый key и не повторять неизвестную запись вслепую. Scoped receipts не отменяют effectful housekeeping до domain checks, включая disabled VIN writes. Это source-only dispatch proof, не измерение runtime stall/SLO.

### Native dispatch старта VIN-job J1

- `j1_research_vin` использует собственный process-global pool: один worker, один ожидающий вызов, 30 секунд ожидания caller; thread создаётся при submit. Capacity остаётся отдельной от остальных J1, read/web/control pools. Это независимое admission, не DB-lock priority, общая сериализация J1 или гарантия progress. Native callback только ставит job в очередь; отдельный serial collector, service lifecycle, SIGTERM/SIGINT и browser SIGALRM/main-thread guard остаются прежними и не запускаются executor.
- Transport refusal имеет известную `schema=autostop.j1.vin_research.v1`, `ok=false`, object `error.code=j1_vin_executor_unavailable`; причины `busy`, `wait_timeout`, `execution_failed`, `executor_unavailable`, `executor_draining`, неизвестная причина становится `executor_unavailable`. Нет DB family routing или отражения VIN/key/receipt. Required vin/key остаются str, без новых defaults/caps/coercions. Whole-tool hints сохраняются false/true/false/true (readOnly/destructive/idempotent/openWorld). Default enabled=0 и housekeeping до domain checks остаются прежними.
- Target остаётся exact normalized supplied VIN: strip/upper и фиксированный план не заменяются representative VIN каталога; PartsAPI group_match/candidate semantics остаются отдельным механизмом и не доказывают factory fitment. Key syntax/sensitivity, same-live-key+exact-VIN lookup до нового busy/capacity admission и foreign-VIN conflict сохраняются. Queue `BEGIN IMMEDIATE`, private initialization до queue commit, rollback+private cleanup при failed queue commit и per-call connection close не меняются. Private0700 tmpfs/database0600/nofollow/regular-file/expiry24h и budgets12 queries/60 docs/6 browser/12 OCR/300 sec/128MiB остаются прежними; persistent stub не содержит VIN/key/queries, recipients/exact-scope/URL/DNS/browser guards не меняются.
- Started failure/timeout, включая finished exception, имеет `outcome_uncertain=true`; caller cancellation распространяется и не означает отмену job, worker stop или rollback. Slot остаётся занят до реального completion; abandoned queued call не стартует, automatic retry отсутствует. После неизвестного исхода сначала сверяй точный receipt/состояние исходных VIN/key; не создавай новый key и не повторяй запись вслепую. Scoped receipt не делает housekeeping idempotent. Это source-only dispatch proof, не runtime stall/SLO или подтверждение заводской комплектации.

Static search использует loopback `AUTOSTOP_J1_SEARXNG_URL`, может иметь fallback. Fetch HTML/PDF соблюдает robots/rate limits и URL/privacy guards. Static J1 работает без browser. Browser использует Unix socket renderer и отдельный egress proxy; запрещены login/cookies/CAPTCHA. Проверяются Docker networks, DNS/private-address guards, socket и SHA-bound marker `root:root 0600`. Отсутствие prerequisite отключает browser path; marker вручную не создаётся.

Для поиска по одному запросу общий бюджет в J1 составляет 18 секунд, включая SearXNG и резервный поиск DuckDuckGo. Тайм-аут поиска отражается как `search_timeout`. При штатной остановке незавершённый запрос возвращается в очередь. Уже записанные результаты доступны после запуска.

Static HTTP ограничивает время отдельного обмена, включая чтение заголовков, тела,
chunk sizes и trailers; непрерывная передача по байту не продлевает этот deadline.
Это не общий deadline DNS, всех redirect/robots checks и rate-limit ожиданий.
Ответ закрывается и при отказе от чтения тела. OCR сообщает число реально обработанных
страниц, а не всех отрендеренных перед остановкой по лимиту символов. Browser IPC
принимает допустимый Unicode в пределах прежних символьных лимитов и конечных byte caps.

Для локальных проверок изолируй оба `AUTOSTOP_J1_CACHE_DIR` и `AUTOSTOP_J1_VIN_CACHE_DIR`:
ordinary worker вызывает private housekeeping даже при выключенном VIN feature flag.
`release-gates.sh` задаёт disposable paths до callbacks и отключает унаследованный
SearXNG endpoint; специальные VIN tests сами создают свои synthetic tmpfs fixtures.

VIN-mode допускает только scoped static fetch с исходным VIN. URL с VIN в browser запрещён;
обезличенные страницы могут использовать прежний attested browser. Наличие feature flag или
инструмента в `tools/list` не подтверждает runtime readiness, доступность источников или качество расшифровки.

`autostop-j1.service` — static worker, root-only `/var/cache/autostop-j1`; `autostop-j1-browser.service` — one-shot renderer/proxy stack. Active/exited не доказывает container health. CLI `j1_research --help`: worker (`--once`), probe; `j1_browser_verify --help`: probe, attest, release-root/socket/marker. Worker и attest меняют состояние; attest вызывается штатной service после topology check. Installers/start/restart — только разрешённый [выпуск](m2-server-update.md).

Проверки installed revision запускай из `/tmp`. Для MCP probe сначала задай `runtime_python` проверенным Python из действующего `ExecStart` службы `autostop-manager-mcp.service` и разреши current в физический snapshot:

```bash
env PYTHONSAFEPATH=1 PYTHONPATH=/opt/autostop-manager-releases/current AUTOSTOP_J1_CACHE_DIR=/var/cache/autostop-j1 AUTOSTOP_J1_SEARXNG_URL=http://127.0.0.1:8890 /usr/bin/python3 -m autostop_manager.j1_research probe
env PYTHONSAFEPATH=1 PYTHONPATH=/opt/autostop-manager-releases/current /usr/bin/python3 -m autostop_manager.j1_browser_verify probe
manager_snapshot=$(readlink -f /opt/autostop-manager-releases/current)
env PYTHONSAFEPATH=1 PYTHONPATH="$manager_snapshot" "${runtime_python:?Set verified service Python}" -m autostop_manager.cli mcp-probe --url http://127.0.0.1:41931/mcp --timeout 90 --browser-check
```

Нужны cache_ready, browser_ready и browser_containers_ready, совпадение browser release/attestation revision, `checks.fetch_page_browser.ok=true`. Для отдельно порученного synthetic static вызова годятся Example Domain/example.com без персональных данных.

В J1 probe `search_configured` подтверждает наличие и валидный адрес SearXNG, а HTTP 200 на его корневом маршруте подтверждает доступность локального HTTP. Для сбоя поиска сверяй `search_failures`, `search_attempts` и время ошибок внешних поисковых систем в журнале SearXNG. В отчёте сохраняй только агрегаты и технические категории без текстов запросов и URL. Если в сохранённом задании нет `search_attempts`, укажи, что причина отдельного запроса не подтверждена.

Browser activation внутри coordinated deploy требует `MemAvailable >=2 GiB`, `SwapFree >=1 GiB`, нулевой `pswpin/pswpout` за 60 секунд и повторной проверки capacity. Старый marker после нового SHA не годится. Диагностика: worker/cache → SearXNG DNS/HTTP → static fetch → browser topology/socket/marker → MCP schema/tool → source. Восстановление использует официальный rollback без ручной подмены release links/marker/persistent cache.
