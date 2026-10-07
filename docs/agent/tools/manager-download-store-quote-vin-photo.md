# manager.download_store_quote_vin_photo — download_store_quote_vin_photo

Справочный/отдельный контур; не automotive workflow

Основной модуль: outside; другие модули: нет.
Классификация: outside. Состояние реализации: implemented.
Источник: manager; первичная база: AutoStop versioned rules or explicit supplied inputs. Исполнение: local_read.

Вызов: native Manager MCP `download_store_quote_vin_photo`.

Входы: `quote_request_id`, `expected_photo_sha256`.
Defaults: `{}`.
Обязательные facade поля: `quote_request_id`, `expected_photo_sha256`.
Fingerprint проверяет [manifest](../manager_mcp_catalog.json); полная inputSchema берётся из регистрации и `tools/list`, в CRM — native_schemas того же bundle.

Синтетический вход (форма, не утверждение о реальном автомобиле/артикуле):
```json
{"expected_photo_sha256":"DEMO","quote_request_id":"DEMO"}
```
Вход, отклоняемый схемой до исполнения инструмента:
```json
{"expected_photo_sha256":"DEMO","quote_request_id":null}
```

Выход: outcome/data/evidence/missing_fields/conflicts/warnings/execution (новые helpers); legacy поля сохраняются.
Ошибки и неполнота: invalid_input сохраняет ошибочный вход как отказ; empty/partial/unsupported не success; configuration/auth/quota/provider/parse ошибки различаются для сетевого источника.

- Назначение и полномочия определяет профильный контур; automotive smoke не выполняет записи.
- Наличие в общем native inventory не разрешает business action или чтение клиента.

Подробный контракт: [справочник](../references/manager-runtime.md). Карточка и inputSchema согласованы versioned export; ручной цвет не является результатом проверки.
