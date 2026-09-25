# J1: публичный поиск, исследование и изолированный браузер

Проверено по source `4e26c6c336d9ff60770803e940860881cdd69615` 2026-09-25: [`j1_research.py`](../../../autostop_manager/j1_research.py), [`j1_fetch.py`](../../../autostop_manager/j1_fetch.py), [`j1_sources.py`](../../../autostop_manager/j1_sources.py), [`j1_browser.py`](../../../autostop_manager/j1_browser.py), [`j1_browser_verify.py`](../../../autostop_manager/j1_browser_verify.py), [`j1_web_research.md`](../j1_web_research.md). J1 принимает только неперсональные публичные запросы. Он не владеет CRM/Store записями, не подтверждает диагноз, OEM или применимость детали. VIN/OEM и PartsAPI маршруты находятся в отдельной карточке каталога; найденная веб-страница не доказывает fitment.

## Локальные каталоги запчастей

`search_offline_parts_catalogs` в source revision — read-only поиск по приватному индексу, **не J1-поиск**. Код: [`offline_catalogs.py`](../../../autostop_manager/offline_catalogs.py), команда подготовки: `scripts/import_offline_parts_catalogs.py --archive ZIP --cache-root PATH --verify-only`, подробный порядок: [`offline_parts_catalogs.md`](../../offline_parts_catalogs.md). `query` обязателен, `catalog_id`, `brand`, `limit` необязательны; полный VIN отклоняется. Фрагмент PDF/XLSX — лишь кандидат, источник/страницу или лист/строку и применимость надо перепроверить. Root по умолчанию — `offline_parts_catalogs` рядом с `AUTOSTOP_MANAGER_DB`; `AUTOSTOP_OFFLINE_CATALOG_ROOT` может задать абсолютный общий root в отдельном release.

Технический снимок 2026-09-25: `/opt/AutostopManager/data/offline_parts_catalogs/catalog_index.json` существует, 35 entries, 53 182 байта; кэш занимает 595 МБ. Архив `autostop-parts-catalogs-20260925-public.zip` размером 373 251 668 байт имел SHA-256 `b2b1e40510e91579a5e8e4be90e0d4a2b461122b6a73af27ec4600d4a1ff7ffe` и содержал 25 каталогов. `--verify-only` сообщил `planned_new_catalogs=0`: все архивные каталоги уже представлены в индексе, повторный импорт не нужен. Свободно было 12 GiB, но это не разрешение на импорт. Runtime MCP `ec173eb99c0d` ещё не объявлял этот новый tool; после выпуска проверяйте `tools/list`/schema и synthetic запрос. Не копируйте исходные каталоги в Git/документацию.

## Маршрут выбора

Для короткого запроса используйте Manager MCP `search_web_multi` и `fetch_page_excerpt`; если нужна JS-страница и готова аттестация, `fetch_page_browser`. Для широкого исследования используйте `j1_research_start` → `j1_research_status` → `j1_research_results`/`j1_research_document` → `j1_research_report`; `j1_research_add_queries` уточняет job, `j1_research_cancel` закрывает её. В `automotive_context` допустимы `make`, `model`, `year`, `engine`, `system`, `symptom`, `dtc`, `part_number`; полный VIN, контакты и секреты запрещены. Профиль автомобиля ограничен 12 запросами/60 страницами, общий режим — 30/300; временный кеш ограничен 250 МБ/7 днями. Схемы аргументов этих MCP tools перечитывайте через активный `tools/list`.

| Компонент | Вход, выход, эффект и проверка |
| --- | --- |
| `j1_research.start_research`/worker | Создаёт job в `AUTOSTOP_J1_CACHE_DIR` и собирает публичные результаты через SearXNG или fallback; запись только временного кеша. Synthetic start меняет кеш и может читать публичную сеть; после него cancel/readback. |
| `research_status`, `research_results`, `research_document`, `research_report` | Читают job по точному ID. Отчёт `autostop.j1.report.v1` различает A/B/C/D источники и `exact`/`analog`/`general`, показывает пробелы. Проверять в disposable cache; не интерпретировать число найденных ссылок как подтверждение. |
| `research_add_queries`, `research_cancel` | Меняют только одну временную job; нужен текущий job ID и readback. Никогда не использовать чужой ID в synthetic smoke. |
| `j1_fetch.search_public` | Static search через `AUTOSTOP_J1_SEARXNG_URL` (loopback); внешний поиск read-only, сеть/провайдер может отказать независимо от MCP. Проверять разрешение DNS/HTTP провайдера отдельно от cache probe. |
| `j1_fetch.fetch_document` | Static HTML/PDF чтение, robots, rate limit, URL/privacy guards; может вызвать browser fallback. Synthetic static: публичная `https://example.com/`; различать static success и fallback. |
| `j1_browser.render_page` | Публичная страница через Unix socket renderer + отдельный egress proxy; сетевое чтение. Отказ при отсутствии активного release SHA, root-owned `0600` marker, socket или аттестации; это штатный fail-closed. Не использовать login/cookies/CAPTCHA. |
| `j1_browser_verify.probe_browser_stack` | Read-only проверяет контейнеры renderer/proxy, сети, socket и marker. `browser_ready=true` в простом J1 probe без `browser_containers_ready` недостаточно; выполняйте verifier probe. |
| `j1_sources.classify_source` | Классификация доказательств A/B/C/D; локальная read-only функция. Тесты проверяют ранжирование, а не истинность конкретной веб-страницы. |

## CLI, сервисы, зависимости

Точный parser: `python -m autostop_manager.j1_research --help` (`worker [--once]`, `probe`) и `python -m autostop_manager.j1_browser_verify --help` (`probe`, `attest`, `--release-root`, `--socket`, `--marker`). `worker` и `attest` меняют состояние; `probe` — read-only. `attest` создаёт marker только после полной проверки topology и должен вызываться штатным `autostop-j1-browser.service`, вручную marker не создавайте.

| Unit / скрипт | Проверка и восстановление |
| --- | --- |
| `autostop-j1.service`, `scripts/install-j1-worker.sh` | Worker с root-only cache `/var/cache/autostop-j1`; зависимость `AUTOSTOP_J1_SEARXNG_URL`. `systemctl is-active autostop-j1.service` и `j1_research probe` проверяют процесс/SQLite, но не фактическую поисковую выдачу. Installer — только официальный release/standalone recovery. |
| `autostop-j1-browser.service`, `scripts/install-j1-browser-stack.sh`, `scripts/run-j1-browser-stack.sh`, `scripts/attest-j1-browser-stack.sh` | One-shot stack с Docker renderer/proxy, private network, socket и release-bound marker. `systemctl is-active`, затем `j1_browser_verify probe`, затем synthetic `mcp-probe --browser-check`. Start/restart только по release runbook; не считать `active (exited)` здоровьем контейнеров. |
| Manager MCP `fetch_page_browser` | Вызывается Codex через `/mcp`; источник схемы — `tools/list`. Проверяет всю цепь Manager → socket → renderer/proxy → публичный сайт. |

Безопасные команды для **установленного** runtime, запуск из `/tmp`:

```bash
env PYTHONPATH=/opt/autostop-manager-releases/current AUTOSTOP_J1_CACHE_DIR=/var/cache/autostop-j1 AUTOSTOP_J1_SEARXNG_URL=http://127.0.0.1:8890 /usr/bin/python3 -m autostop_manager.j1_research probe
env PYTHONPATH=/opt/autostop-manager-releases/current /usr/bin/python3 -m autostop_manager.j1_browser_verify probe
env PYTHONPATH=/opt/autostop-manager-releases/current /opt/AutostopManager/.venv/bin/python -m autostop_manager.cli mcp-probe --url http://127.0.0.1:41931/mcp --timeout 90 --browser-check
```

Ожидайте `cache_ready`, `browser_ready`, `browser_containers_ready`, совпадение `browser_release_revision` и `browser_attestation_revision` и `checks.fetch_page_browser.ok=true`. Во время проверки 2026-09-25 установленный SHA `ec173eb99c0d` дал все эти признаки; это снимок до последующего выпуска. Дополнительно synthetic static `search_public('Example Domain site:example.com', searxng_url='http://127.0.0.1:8890')` вернул провайдер `searxng` и 4 результата; `fetch_document('https://example.com/', allow_browser=False)` дал `ok=true`, `extraction_method=html_text`. `j1_research probe` проверяет cache/schema и browser-status, **не** делает поисковый запрос. Live search quality, provider quotas и точность источников этим не измерены.

Перед coordinated release с новым Manager SHA browser activation требует отдельного gate из `/opt/autostopcrm/deploy.sh`: `MemAvailable >=2 GiB`, `SwapFree >=1 GiB`, без изменений `pswpin/pswpout` за 60 секунд, затем повторная проверка ёмкости. Два read-only окна 2026-09-25 показали достаточную ёмкость, но `pswpin` вырос на 62 и 5208 страниц соответственно; **на момент проверки gate не пройден**. Опция `AUTOSTOP_J1_BROWSER_ACTIVATE_ON_DEPLOY=1` при таком состоянии должна fail-closed пропустить браузер и сохранить static J1. Для полного browser восстановления нужен успешный gate внутри deploy; старый SHA-bound marker после смены ревизии не является аттестацией нового стека.

Тесты: `tests/test_j1_research.py`, `tests/test_j1_fetch_stage2.py`, `tests/test_j1_browser_stack.py`, `tests/test_j1_browser_verify.py`, `tests/test_j1_evidence_search.py`, `tests/test_j1_install.py`. При отказе порядок: worker/SQLite → SearXNG DNS/HTTP отдельно → static fetch → browser service/topology/socket/marker → Manager MCP schema/tool → источник. Для браузерного запуска соблюдайте memory/swap preflight из [`deployment_runbook.md`](../deployment_runbook.md), официальный rollback, без ручной подмены marker, release symlink или persistent cache. Не сохраняйте тексты страниц в общих docs или Manager memory.
