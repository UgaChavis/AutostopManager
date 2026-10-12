# E1 modernization: архитектура, задачи и приёмка

Поручение E1_FULL_IMPLEMENTATION_PROMPT_RU.md; baseline Manager2deb6231368cc8f36fa3c1bde75d5655fe95bee7, CRM2394da173094872d371bed3b4d6ae1d8009945f8.
Root Manager496ee WIP VIN не перенесён второй раз: его исправления уже опубликованы в baselinePR66.
Dirty main bytes/index/stashes сохраняются; feature source изолирован. Store API/source не меняется.
Технический прогресс/receipts находятся root-private /var/lib/autostop-manager/private/e1-modernization-20261006.
Customer payload/secret здесь отсутствуют; журнал M2 обновляется после проверенного внедрения.

## Архитектура

- E1 — карта независимых задач; E2–E15 parent E1, устойчивый module_key отдельно от E-code.
- automotive_tools.json + JSON Schema — metadata; Markdown правила; code/MCP inputSchema — authoritative.
- Versioned exporter использует exact git archive; source_revision отделён от deterministic content_hash.
- 43 PartsAPI operations остаются одним facade, 19 новых native helpers; старые names/contracts сохраняются.
- automotive_contracts — binding/envelope/namespace validation; identity/parts/labor/offline независимы.
- Local WMI/platform/brand/frame используют один реестр, vininfo/Corgi — подготовленные pinned dependencies.
- CRM committed readonly bundle; manual tool_statuses в существующем durable graph, owner/CAS/idempotency.
- Большое центральное окно viewmode безопасно отображает MD text/card/schema/status; editmode прежний.
- Root migration atomicreplace freshgraph меняет E-блок и необходимые endpoints; не-E bytes/canvas/statuses сохраняются.
- schema→Manager publication→export publishedpin→CRM finalgates/publication→coordinated deploy→migration→cleanup.

## Матрица работ

| Фаза | Результат | Владелец |
| --- | --- | --- |
| P0 | git/source/runtime/host/provenance/current+rollback baseline | root/release engineer |
| P1–P2 | общий registry/schema/миниинструкции/E1–E15/nav/export | root |
| P3 | pure helpers/reuse/validators/selected acquisition | Manager engineer |
| P3 local | official licenses/pinned packages/preparation/offline proof | offline engineer |
| P4 | CRM service/API/Gateway/status/modal/parity/backup semantics | CRM engineer |
| P5 | integration/localfullgates/migration preservation/independent review | root+team |
| P6 | separate Manager+CRM PRs/exactmergedCI/publishedpin | root |
| P7 | freshfullbackup/preflight/cleanreleaseSources/coordinateddeploy/migration | root |
| P8 | protectionpolicy/nativecleanup/independentinstalled/UI/readiness | root |
| P9 | C01–C21 evidence/M2/runbook/final owner report | root |

## Критерии

C01 map/nav/graph; C02all43methods+actualnative; C03 exactcards; C04bundleorigin/hash;
C05 executablehelpers; C06reuse/nohiddenreads; C07actualoffline; C08purezeronetwork;
C09 namespaces/unknown/variants/quantity; C10 evidenceboundOEM/fitment; C11modalaccessibility;
C12allcards/provider/sharedID; C13statusguards/CAS/replay/persistence; C14graphpreservation;
C15fullrestorevsportable; C16local+exactGitHubCI; C17published/installedparity;
C18MCP/CRM/Store/J1/Telegram/scheduler; C19nativecleanup/protection; C20readiness;
C21other-engineer runbook/rollback.
Каждый результат получает private PASS/FAIL/BLOCKED_EXTERNAL + timestamp/revision/evidence;
статус этой planning записи не является runtime приёмкой и не заменяет source/GitHub/installed/UI evidence.

## Повторение и откат

Working runbook — docs/agent/references/automotive-tools.md и deployment.md.
Export после Manager merge/CI, final CRM pin+gates после export; publish SHA проверяется live remote.
Coordinated deploy.sh сохраняет backup/hold/duty/immutableversions и штатный rollback.
Fullbackup содержит tool_statuses; portabletemplate их исключает; graphreplace map сохраняет.
No broad prune; cleanup exact freshplan/hash с current+previous coherenttuples.
Если cutover partial — штатный workingtuple rollback; после открытияwriters старая DB не накладывается.
Полная цель завершается только после всех required C01–C21, внешняя dependency не PASS.
