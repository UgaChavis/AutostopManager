# manager.fetch_page_excerpt — Ограниченный текст публичной страницы

Ограниченный текст публичной страницы

Основной модуль: [E15](../modules/E15.md); другие модули: нет.
Классификация: active. Состояние реализации: implemented.
Источник: public_web; первичная база: primary URL lineage must be preserved. Исполнение: network_read.

Вызов: native Manager MCP `fetch_page_excerpt`.

Входы: `url`, `max_chars`.
Defaults: `{"max_chars":2500}`.
Обязательные facade поля: `url`.
Fingerprint проверяет [manifest](../manager_mcp_catalog.json); полная inputSchema берётся из регистрации и `tools/list`, в CRM — native_schemas того же bundle.

Синтетический вход (форма, не утверждение о реальном автомобиле/артикуле):
```json
{"max_chars":1200,"url":"https://example.com/"}
```
Вход, отклоняемый схемой до исполнения инструмента:
```json
{"url":null}
```

Выход: Legacy flat web gateway: ok/schema/capability/read_only/url/final_url/domain/title/excerpt/links/access_flags/requires_human/status_code/mode/vin_redacted.; Failure returns ok=false and error={code,retryable}; no canonical automotive data/evidence envelope.; Known HTTP status, content_type, requested/effective chars, extraction_method and truncated; explicit PDF uses the existing guarded static extractor.; excerpt_limit_chars, extracted_chars/received_chars/delivered_chars и execution сохраняют фактические ограничения; отсутствие счётчика не означает ноль сетевых вызовов.
Ошибки и неполнота: web_page_url_invalid for unsafe/invalid URL; gateway/configuration/response errors and browser_render_failed remain distinct with retryable.; Safe HTTP/transport causes and upstream retryable=false are preserved; unknown legacy cause remains unknown. 404 does not start PDF/browser fallback..

- Reads one bounded public page through installed CRM WebResearchGatewayV1 (E15); max_chars is bounded at 8000.
- No login/captcha bypass; access_flags/requires_human report blocked content. A public page is evidence, not automatic fitment or repair authorization.

Подробный контракт: [справочник](../references/web-research.md). Карточка и inputSchema согласованы versioned export; ручной цвет не является результатом проверки.
