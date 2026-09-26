# Матрица технического аудита, 2026-09-26

Статусы ниже — исходный live-аудит до нового выпуска. PASS относится к конкретной
проверке, не всем возможным бизнес-сценариям. Финальные GitHub/runtime SHA и
независимый post-deploy/post-boot readback сохраняются в закрытом техническом
отчёте `/root/autostop-audit-20260926/`. Данные клиентов в отчёт не входят.

| Цепочка | Контракт / проверка | Результат исходного аудита | Исправление / граница |
| --- | --- | --- | --- |
| Codex → Manager MCP | `mcp-probe`, active tools/list | PASS: 43 tools, схемы, synthetic resolver, provider failure | После выпуска повторить на установленном SHA |
| Codex → CRM Gateway | local/public exhaustive check, OAuth identity | PASS: 24 tools, auth, schemas, все safe invocations | Реальные финансовые изменения не проверяются |
| Manager → Store | runtime/capabilities/search/exact read | FAIL: поиск заказов отклоняет новые поля оплаты | strict контракт `payment_status`, `paid_at`, регрессии search/summary/full |
| Store → поставщики | sourcing ROSSKO/BERG | PASS transport; FAIL достоверность confidence/price basis | Совпадение артикула не подтверждает применимость; без закупочной цены нет confirmed purchase |
| VIN/OEM, PartsAPI | live norms_models, synthetic VIN identity, MANN catalog | PASS запросы/структура результатов | Кандидаты не доказывают VIN-specific fitment или заказ |
| Локальные каталоги | `search_offline_parts_catalogs`, pinned release sync | PASS bounded search with references | Сам поиск не подтверждает полноту всех каталогов |
| J1 static | queued synthetic public job → search → fetch → report | PASS worker/SearXNG/HTML; дубликаты отмечены отдельно | Качество внешних источников проверяется по задаче |
| J1 browser | native `mcp-probe --browser-check` и verifier | FAIL: browser release/attestation от старого SHA | Штатная activation нового SHA с memory/swap gate, затем MCP render |
| Telegram → bridge → wake | content-free status, duty, VAD/inference self-check | PASS: authorized, wake connected, queue empty, voice execution | Клиентские text/photo/voice сценарии — synthetic tests; live sends не разрешены аудитом |
| G1 → scheduler/timers | readiness + пять таймеров | PASS: in_sync, digest OFF, no hold/outbox | Ошибка одного systemctl изолируется; backup подписан Store; справки G1 без запуска |
| G1 / карта CRM | Playwright desktop/mobile/keyboard | Новые тесты справок и отсутствия пересечений подписей | До post-deploy readback это результат тестового контура |
| Windsor → Instagram | get_connectors/fields/actions/data; exact wake discovery/read | PASS: чтение в интерактивном Codex, CLI и wake | Записи BLOCKED для live smoke: тестовые публикации запрещены; Direct/event trigger отсутствуют |
| Gmail | profile/labels/search read; full doctor proof | PASS чтение; BLOCKED свежая проверка доставки | Квитанция self-delivery старше 30 дней; не освежать её без реального разрешённого теста |
| Backup/recovery | новый pg_dump + full decompress + isolated restore | Результат в закрытом restore-drill.log | Только network-none временные ресурсы; проверить cleanup и production health |
| MNG1 release | canonical CI → exact SHA → штатный deploy → boot/readback | Выполняется после source gates | Локальные tests/PR не подтверждают установленную систему |
| VPN | hostname/SSH/container status | FST PASS процесс; MNG2 BLOCKED точная идентификация доступа | MNG2 основной, FST резервный по владельцу; обновления VPN исключены |

Операционные карточки и восстановление: [каталог](README.md),
[runbook](../deployment_runbook.md), [runtime/hosts](runtime_release.md),
[Instagram](instagram.md). Для каждой неисправности различайте transport,
контракт, разрешения и результат; отсутствие ошибки HTTP не заменяет readback.
