# J1: публичное исследование и браузер

Сценарий: [J1](../modules/J1.md). Source: [j1_research.py](../../../autostop_manager/j1_research.py), [j1_fetch.py](../../../autostop_manager/j1_fetch.py), [j1_sources.py](../../../autostop_manager/j1_sources.py), [j1_browser.py](../../../autostop_manager/j1_browser.py), [j1_browser_verify.py](../../../autostop_manager/j1_browser_verify.py). J1 читает публичные материалы, не подтверждает диагноз/OEM/fitment и не владеет CRM/Store.

## Выбор и отчёт

Короткий запрос: `search_web_multi` → `fetch_page_excerpt`; JS-страница — `fetch_page_browser` при готовом browser. Исследование: `j1_research_start` → status → results/document порциями → report (`autostop.j1.report.v1`). `add_queries/cancel` меняют только точную временную job, затем требуется status readback.

Automotive context: `make`, `model`, `year`, `engine`, `system`, `symptom`, `dtc`, `part_number`; полный VIN, контакты и секреты не передаются. Automotive profile — до 12 запросов/60 страниц; общий — 30/300. Лимиты: 50 000 символов документа, cache 250 МБ/семь дней. `canonical_url` удаляет только tracking; дубли не индексируются. Sources: A — OEM/регулятор/TSB, B — component manufacturer, C — catalog, D — форум/контекст. D не доказательство; частота оценивается лишь по измеримой совокупности A/B, иначе `not_measured`. Отчёт различает `exact/analog/general`, доступ, confidence и gaps.

`start` создаёт job в `AUTOSTOP_J1_CACHE_DIR` и читает сеть. Status/results/document/report read-only; jobs не являются долговременной customer memory. Локальный `search_offline_parts_catalogs` — отдельное чтение файлов, не J1: [offline-catalogs.md](offline-catalogs.md).

## Runtime и guards

Static search использует loopback `AUTOSTOP_J1_SEARXNG_URL`, может иметь fallback. Fetch HTML/PDF соблюдает robots/rate limits и URL/privacy guards. Static J1 работает без browser. Browser использует Unix socket renderer и отдельный egress proxy; запрещены login/cookies/CAPTCHA. Проверяются Docker networks, DNS/private-address guards, socket и SHA-bound marker `root:root 0600`. Отсутствие prerequisite отключает browser path; marker вручную не создаётся.

`autostop-j1.service` — static worker, root-only `/var/cache/autostop-j1`; `autostop-j1-browser.service` — one-shot renderer/proxy stack. Active/exited не доказывает container health. CLI `j1_research --help`: worker (`--once`), probe; `j1_browser_verify --help`: probe, attest, release-root/socket/marker. Worker и attest меняют состояние; attest вызывается штатной service после topology check. Installers/start/restart — только разрешённый [выпуск](deployment.md).

Проверки installed revision запускай из `/tmp`:

```bash
env PYTHONSAFEPATH=1 PYTHONPATH=/opt/autostop-manager-releases/current AUTOSTOP_J1_CACHE_DIR=/var/cache/autostop-j1 AUTOSTOP_J1_SEARXNG_URL=http://127.0.0.1:8890 /usr/bin/python3 -m autostop_manager.j1_research probe
env PYTHONSAFEPATH=1 PYTHONPATH=/opt/autostop-manager-releases/current /usr/bin/python3 -m autostop_manager.j1_browser_verify probe
env PYTHONSAFEPATH=1 PYTHONPATH=/opt/autostop-manager-releases/current /opt/AutostopManager/.venv/bin/python -m autostop_manager.cli mcp-probe --url http://127.0.0.1:41931/mcp --timeout 90 --browser-check
```

Нужны cache_ready, browser_ready и browser_containers_ready, совпадение browser release/attestation revision, `checks.fetch_page_browser.ok=true`. J1 probe не делает search; для отдельно порученного synthetic static вызова годятся Example Domain/example.com без персональных данных. Green probe не доказывает качество выдачи/источника.

Browser activation внутри coordinated deploy требует `MemAvailable >=2 GiB`, `SwapFree >=1 GiB`, нулевой `pswpin/pswpout` за 60 секунд и повторной проверки capacity. Старый marker после нового SHA не годится. Диагностика: worker/cache → SearXNG DNS/HTTP → static fetch → browser topology/socket/marker → MCP schema/tool → source. Восстановление использует официальный rollback без ручной подмены release links/marker/persistent cache.
