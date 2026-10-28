# Клиентский кандидат после независимого ревью

Рабочие ветки: Manager `codex/host-catalogs-20261028`, CRM
`codex/manager-catalog-c3b49a-20261028`. Начало исправлений — чистые Manager
`f8f2b027554f4015bbe8f6434182bea41a122a75` и CRM
`0e0cbf6b6329f17b22b3658d7e5c3b1a238b000b`.
Ранее подготовленные commits сохраняются; primary checkout и другие WIP не меняются.

Поручение: полностью устранить замечания независимого ревью и подготовить локальные
коммиты к слиянию. Публикация, само слияние, установка и рабочие тестовые эффекты
в этот этап не входят. Cwd остаётся `/opt/AutostopManager`.

## Изменения по замечаниям

| Замечание | Изменение и проверяемое поведение |
| --- | --- |
| R1: пауза теряет вопрос | `waiting_control` сохраняет открытые события; resume продолжает их после terminal proof, ACK не повторяется |
| R2: продолжение до defer вызывает поздний replay | Отказ и освобождение событий с уже принятым продолжением обрабатываются атомарно |
| R3: выполненная подсказка блокирует после рестарта | Native job-read фиксирует прочитанную ревизию; успешный актуальный результат снимает её pending, потерянная незавершённая подсказка остаётся под защитой |
| R4: ручной ответ превышает локальный RPC-бюджет | Пакетное чтение точных message IDs и сетевой бюджет 20/30с вместо локального 2с |
| R5: hint-маркер скрывает сильное противоречие | Публичный флаг не даёт исключения; настоящие caller/provider-противоречия блокируются, слабые подсказки сохраняют provenance |
| R6: отказ источника блокирует полезный merged | Пустые привязанные отказы сохраняются в `source_failures`; merged проходит ready/native planner, чужие привязки и реальные конфликты не скрываются |
| R7: CRM пишет неоднозначный alias | Shared service сначала разрешает точный/единственный ID, сохраняет canonical vehicle.id; неоднозначность409 до изменения файлов, Gateway сверяет canonical результат |

Дополнительно исправлены реальные входы Telethon service/contact events и
согласован single native decode с batch для пяти дополнительных полей профиля.
Формулировка root-only ACL уточнена: системный UID не является отдельной изоляцией
между root worker и root-координатором; полномочия берутся из доверенного Codex.

## Доказательства и границы

Используются существующие dev dependencies и изолированные synthetic fixtures,
JsonStore/SQLite, реальные локальные Gateway/Telegram handlers и временный Unix RPC.
Контрольные сценарии включают replay/unknown, точные IDs и версии, сохранение прежних
полей, чужой/неоднозначный автомобиль, паузу и рестарт, сильные противоречия профиля.

Новый исполняемый crossrepo сценарий потребляет ранее подготовленные предложения
клиентского поведения и проверяет фактические CRM/Telegram эффекты в закрытом стенде.
Решения модели в нём контролируемые; это не live-приёмка диалога или доказательство
применимости реальной фотографии/детали. Динамическая редактура произвольных изображений
и документов остаётся обязанностью исполнителя по источнику и полному клиентскому скиллу.

Локальные проверки:

- Telegram: 14 новых native regression cases; 373 теста затронутых потребителей
  прошли. После проверки новых веток parking/restart/proof и поздней отправки
  прошли адресные повторы; status/active/unknown subset — 18 passed.
  Перекрывающиеся прогоны не суммируются как отдельные уникальные случаи.
- Automotive: 340 affected trust/helpers/regression cases прошли на окончательных
  исправлениях, включая 16 новых случаев provenance и обоих носителей годовых
  альтернатив. Ещё 185 decoder/input/semantics cases прошли ранее в этом этапе;
  их неизменённые ветки не повторялись без основания.
- Manager catalog/schema/manifest: 86 passed. Native inventory — 72 инструмента,
  fingerprint `4b8897f3c0c4857cc0da18bfc57b50f41e9d9a384d0bc372f64d62e97db108de`.
  Registry, generated tool card и defaults обновлены по фактической inputSchema.
  Project instruction inventory — без mismatch/orphan.
- CRM: 39 адресных service/Gateway/API/lifecycle/search cases passed. Проверяются
  exact/unique/ambiguous ID, канонический persist, чужой автомобиль и stale revisions;
  при отказе реальные JSON-файлы остаются побайтно прежними.
- Crossrepo `scripts/check_client_handoff.py`: три сценария прошли через настоящие
  локальные handlers. Проверены explicit preliminary consent, правильная parts_store
  карточка, сохранение истории/меток/заказ-наряда, независимый readback, повтор одного
  ключа без второй записи, одна проверенная передача и итог клиенту. Неизвестный исход
  CRM/медиа не порождает повтор или ложное подтверждение. Клиентские текст/таблица/
  подписи, одностраничный PDF и JPEG reference проверены отдельно от внутренних цен;
  upload/readback байты совпадают, временные файлы удалены, сетевых попыток — 0.
- Повторная независимая проверка тремя ревьюерами подтвердила R1–R7 и сохранение
  unknown/lease/receipt guards. Особо проверены restart до proof, ранний resume,
  прочитанная/потерянная/обновлённая подсказка, actual Unix RPC bulk read, сильные
  противоречия, matching provenance refs, nested years и отказ CRM до mutation.
  Новых блокеров в этом проверенном объёме не найдено.

Hooks Ruff check/format и `git diff --check` проходят. Прикладные среды и зависимости
не менялись; использованы существующие dev/runtime dependencies и исправный отдельный
cache hooks. Полный CI, Windows rehearsal, нагрузочные тесты и release builds не выполнялись.

Локальные коммиты реализации:

- Manager `e9376ff2909d21ff7823d4cbe1fd03615d02c1fb` — Telegram R1–R4, service events,
  shutdown/restart/resume и regression tests.
- Manager `f503881e4d307694e8ad9883e89aac02f3a2e979` — R5/R6, provenance/year carriers
  и native single/batch parity.
- CRM `35e5f6d41fea37b15c1d77e9302f83227ac57647` — R7 и shared service/API/Gateway tests.
- CRM `68221ec29d2cd0dfccef54aa58aaa51fc812374b` — исполняемый crossrepo стенд и его команда.

Инструкции и native manifest фиксируются отдельным локальным коммитом Manager.
Затем CRM package формируется из этого точного HEAD, проверяется сравнением source/native
schemas и сохранностью остального графа. Финальный прогон crossrepo по чистым committed
источникам и readback pin входят в техническую передачу; точные конечные SHA — `git rev-parse HEAD`
в двух указанных checkout и запись результата M2.

## Подготовка слияния

При начале этапа read-only Git проверка показала основные ветки:

- Manager `AutostopManager` → `c3b49a7e432c856ff514802950f16f61cddaf588`.
- CRM `autostopcrm-v1` → `60b4ca39e841269e8c1eb5a4f25e1c8472c9458f`.

Перед передачей проверяются ancestry/отсутствие конфликтов на этих точных вершинах,
чистота рабочих веток и совпадение CRM instruction/tool package с конечным Manager SHA.
Рабочий Manager остаётся `/opt/autostop-manager-releases/20261027T-M1M2-c7cd1f69`,
REVISION `c7cd1f69ee5cfe3ae80e1b8bc02f1019b2cc2099`.
Дежурства M1/M2 не включаются. Live effects, CI release-image и migration/rollback
требуют соответствующего следующего этапа.
