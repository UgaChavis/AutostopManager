# Release manifest: 2026-09-25

Авторитет: Git remote refs и активные artifacts, проверенные 2026-09-25. Это решение о составе Manager release candidate. Итоговые CI run, exact candidate SHA и production readback фиксируются после публикации и выпуска отдельно; этот документ не объявляет релиз состоявшимся.

| Система | GitHub production ref | Установленный SHA до выпуска | Решение |
| --- | --- | --- | --- |
| Manager | `AutostopManager` = `2064711eecb8646a2fa5d91a9d874a86d3b42f9d` | sealed `REVISION=ec173eb99c0da1685f1b8381e9afe81fc6811086` | Включить проверенные опубликованные commits ниже и новые operation cards через зелёный PR; coordinated CRM deploy после gates |
| CRM | `autostopcrm-v1` = `ac10f5340c21554be8f2bd8f3572b58bbe355ee4` | OCI revision `5f2fb21911914bfd7b4f2b979d192ed1a5e35d52` | PR #11 уже включил pinned catalog sync и default Manager MCP activation; опубликовать после общего gate вместе с operation cards |
| Store | `main` = `1c4734ac7b9939106b432397267f133ab36b9125` | deploy marker/image `1c4734a` | Runtime уже на ref; новые Store operation cards будут опубликованы через PR. Любой merge в `main`, даже docs-only, запускает полный официальный `Deploy VPS`, поэтому требуется его CI, backup/cutover gate и post-release readback |

Manager GitHub `quality` для `4e26c6c` завершился success (run 36112066255); PR #19, #20, #21 и #22 имели успешные quality checks и были merged. `2064711` добавил закреплённую автоматическую синхронизацию каталога до maintenance. Ниже SHA из `ec173eb..origin/AutostopManager`, все относятся к PartsAPI/offline catalog выпуску:

Опубликованный архив `catalogs-2026-09-25` (373251668 B) совпал по SHA-256 с локальным файлом. Production cache `data/offline_parts_catalogs/catalog_index.json` содержит 35 записей; pinned release sync `--verify-only` подтвердил 25 каталогов, OCR и synthetic search (`status=ready`). Повторный импорт не входит в release; после активации нового MCP нужен отдельный read-only smoke поиска.

| Commit SHA | Исходная ветка/PR | Содержание | Решение |
| --- | --- | --- | --- |
| `9eba52681f9fe6b0a8e8952aa656dc750d145d5d` | `codex/partsapi-shop-contract-20260925`, #19 | текущий PartsAPI shop contract | include |
| `782f548e35b60603e1fcb47da213a7e36147fb85` | `codex/offline-catalog-knowledge-20260925`, #20 | импорт и поиск offline каталогов | include |
| `809e8e07e9295f90f9a292a7ce96effa8c5641e3` | #20 | MCP count tests | include |
| `9ac6ef9d7d4f4456f3470e2bca53576903055565` | #20 | synthetic catalog/PDF tests | include |
| `f2e527a5bfda3766e8cc3972a094e7c7010a3e74` | #20 | `ripgrep` для CI проверки каталога | include |
| `8471a5a8f7b17c44c41fc073b90a8b86871f90e8` | merge #19 | PartsAPI merge provenance | include |
| `221110558b849f4083e1d0424d4f7d0e2de9aca6` | #20 | PartsAPI + offline catalog integration merge | include |
| `3c89acf3aeae550c263c1e39b0def39aa5e10275` | merge #20 | catalog merge provenance | include |
| `d90402bb936e7cf2c3b14bf5228124ca217427c1` | `codex/catalog-release-link-20260925`, #21 | ссылка на опубликованный архив | include |
| `4e26c6c336d9ff60770803e940860881cdd69615` | merge #21 | основной ref после catalogue release | include |
| `1b5ca8d5de61b6adf604246d147be86170743a5c` | `codex/catalog-auto-deploy-20260925`, #22 | pinned catalog sync перед maintenance | include |
| `26355bc4b00af273da45645e58202cbae0b7b9f3` | #22 | release docs и instruction budget | include |
| `2064711eecb8646a2fa5d91a9d874a86d3b42f9d` | merge #22 | основной ref после catalog auto-sync | include |

Отдельные опубликованные Manager `codex/*` после сравнения с текущей веткой:

| Branch tip | Решение и основание |
| --- | --- |
| `5041eb2` remote-v2 integration | exclude: managed PC/Remote v2 за периметром текущего восстановления |
| `5d23baf` manager-hardening | exclude: ветка от июля, большой расходящийся diff; текущая основная ветка содержит более новые контракты, без повторной адаптации такой merge не проходит scope/regression gate |
| `5876173` server-maintenance doc sync, open PR #13 | exclude: устаревший формат CRM MCP catalog (current manifest — 24-tool fingerprint), старый count/test нельзя переносить как живой контракт |
| `636f683` Store analytics | exclude: текущая основная ветка уже содержит `get_store_analytics_report`; старая ветка дублирует feature с расходящимися schemas |
| `43e83ca` work Telegram service-user docs | exclude as separate commit: текущий основной ref не содержит старый playbook; действующие service-user маршруты включены в новый module operation catalog, который сверяется с units |

CRM: PR #11 (`e65414c`, merge `ac10f53`) добавил вызов pinned catalog sync до maintenance и включение Manager MCP по умолчанию. Установленный CRM до выпуска всё ещё `5f2fb21`. 11 из 13 ранее опубликованных `codex/*` веток уже предки `autostopcrm-v1`. `codex/completion-act-editor` (`54433ff`) имеет patch-equivalent в основном ref (`40e1db6`), поэтому повторно не включается. `codex/server-maintenance-doc-sync-crm` (`286ef12`, open PR #10) имеет конфликт и старый quality success от 2026-08-21; его изменение удаляло ссылку на retired instruction, которой в текущем `scripts/docs_audit.py` уже нет. Текущий docs audit проходит, поэтому patch устарел и не включается.

Store: production `main` уже содержит 15 `codex/*` refs. `codex/store-command-docs` (`ec682f1`, open draft PR #18) patch-equivalent двум commits текущего `main`, при этом PR conflicting. `codex/integration-hardening-20260719` (`40dff31`) уже включён через merge #16. `codex/store-http2-cache-fingerprints` (`99e4184`) закрыт, адаптированное исправление есть в `d4fbdf8` текущего `main`. Эти ветки не повторяются. `codex/customer-order-display` (`d94ffb5`), `codex/full-audit-cleanup` (`76bf21a`), `codex/work-latest` (`2f5b9ef`) и четыре июльские admin-v2 ветки (`a6a92d3`, `521a870`, `9d49ebb`, `3ce79ba`) расходятся с main и не имеют CI/PR для своих heads: без отдельной ревизии и новых gates они не release-ready. PR #14 (`1f61a68`) конфликтует и затрагивает удалённый Flutter admin; его апрельский CI не годится как текущий gate. Их исключение сохраняет текущий Store runtime без изменений.

Открытые PR не считаются одобренными к выпуску только из-за зелёного CI; conflicts или устаревшие schemas фиксируются явно. Mandatory human approvals, если появятся в GitHub policy, нельзя обходить. Никакие production business records, цены или личные данные в этот manifest не включены.
