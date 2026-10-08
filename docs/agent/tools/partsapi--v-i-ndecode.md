# partsapi.VINdecode — VINdecode

Вызвать VINdecode; VIN decode into TecDoc/TecRMI vehicle identity and characteristics.

Основной модуль: [E2](../modules/E2.md); другие модули: нет.
Классификация: active. Состояние реализации: implemented.
Источник: partsapi_ru; первичная база: declared_by_provider; VIN/TecDoc/maintenance/AUTONORMS namespaces. Исполнение: network_read.

Вызов: native Manager MCP `partsapi_catalog_lookup`.
operation: `vin_decode`.
api_method: `VINdecode`.

Входы: `identifier`, `lang`.
Defaults: `{"lang":"ru"}`.
Обязательные facade поля: `operation`.
Текущая inputSchema и проверка версии — [D1](../modules/D1.md).

Синтетический вход (форма, не утверждение о реальном автомобиле/артикуле):
```json
{"dry_run":true,"identifier":"WVWZZZ1KZAW000001","operation":"vin_decode"}
```
Вход, отклоняемый схемой до исполнения инструмента:
```json
{"operation":[]}
```

Выход: source/parser/outcome и профиль PartsAPI; `group_match` сохраняет исходный VIN, `identifier_matches_request=false`, `binding_kind=provider_group_reference` и `identifier_semantics=group_representative` для каталожного кандидата.
Ошибки и неполнота: invalid_input сохраняет ошибочный вход как отказ; empty/partial/unsupported не success; configuration/auth/quota/provider/parse ошибки различаются для сетевого источника.

- Модельный год отдельно от даты производства; WMI не определяет двигатель.
- VIN представителя группы допускается только этим методом по уточнению владельца; не подтверждает заводскую точную спецификацию или VIN/OEM fitment. Полная политика — в справочнике.
- API method отличается от facade operation; key назначает сервер, secrets не принимаются.
- TecDoc/maintenance/AUTONORMS ID namespace не взаимозаменяемы; указанные IDs в примере синтетические.

Подробный контракт: [справочник](../references/partsapi.md).
