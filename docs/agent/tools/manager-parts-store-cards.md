# manager.parts_store_cards — parts_store_cards

Справочный/отдельный контур; не automotive workflow

Основной модуль: outside; ссылки: нет. Состояние реализации: implemented.
Источник: manager; первичная база: AutoStop versioned rules or explicit supplied inputs. Исполнение: business_write.

Вызов: native Manager MCP `parts_store_cards`.

Входы: `operation`, `query`, `limit`, `card_id`, `title`, `vehicle`, `description`, `note`, `expected_updated_at`, `idempotency_key`.
Defaults: `{"card_id":"","description":"","expected_updated_at":"","idempotency_key":"","limit":30,"note":"","query":"","title":"","vehicle":""}`.
Обязательные facade поля: `operation`.
Fingerprint проверяет [manifest](../manager_mcp_catalog.json); полная inputSchema берётся из регистрации и `tools/list`, в CRM — native_schemas того же bundle.

Синтетический вход (форма, не утверждение о реальном автомобиле/артикуле):
```json
{"operation":"list"}
```
Отрицательный вход:
```json
{"operation":"__invalid__"}
```

Выход: outcome/data/evidence/missing_fields/conflicts/warnings/execution (новые helpers); legacy поля сохраняются.
Ошибки и неполнота: invalid_input сохраняет ошибочный вход как отказ; empty/partial/unsupported не success; configuration/auth/quota/provider/parse ошибки различаются для сетевого источника.

- Назначение и полномочия определяет профильный контур; automotive smoke не выполняет записи.
- Наличие в общем native inventory не разрешает business action или чтение клиента.

Подробный контракт: [справочник](../references/manager-runtime.md). Карточка и inputSchema согласованы versioned export; ручной цвет не является результатом проверки.
