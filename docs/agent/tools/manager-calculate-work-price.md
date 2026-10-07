# manager.calculate_work_price — Расчёт по явной ставке и политике

Расчёт по явной ставке и политике

Основной модуль: [E13](../modules/E13.md); другие модули: нет.
Классификация: active. Состояние реализации: implemented.
Источник: manager; первичная база: AutoStop versioned rules or explicit supplied inputs. Исполнение: pure.

Вызов: native Manager MCP `calculate_work_price`.

Входы: `labor`, `policy`, `hourly_rate`, `observations`, `unknown_costs`.
Defaults: `{"hourly_rate":null,"observations":null,"unknown_costs":null}`.
Обязательные facade поля: `labor`, `policy`.
Fingerprint проверяет [manifest](../manager_mcp_catalog.json); полная inputSchema берётся из регистрации и `tools/list`, в CRM — native_schemas того же bundle.

Синтетический вход (форма, не утверждение о реальном автомобиле/артикуле):
```json
{"hourly_rate":1000,"labor":[{"hours":2,"operation_id":"demo","operation_name":"DEMO работа"}],"policy":{"basis":"hourly_rate","currency":"RUB","version":"demo-v1"},"unknown_costs":["диагностика"]}
```
Вход, отклоняемый схемой до исполнения инструмента:
```json
{"hourly_rate":1000,"labor":null,"policy":{"basis":"hourly_rate","currency":"RUB","version":"demo-v1"},"unknown_costs":["диагностика"]}
```

Выход: Envelope: schema_version/tool_id/ok/outcome/data/evidence/missing_fields/conflicts/warnings/execution; paths ниже относятся к data.; basis/hourly_rate/policy/currency/operations[{operation_id,operation_name,price,range}]; known_subtotal/total/range/exclusions/unknown_costs; policy остается явным входом, version/coefficient/rounding не являются отдельными полями выхода.
Ошибки и неполнота: invalid_input без policy.version/allowed basis/rate или при malformed inputs; partial при неизвестной цене/часах/range/расходах/нерешенном overlap; total и range=null, известная часть сохраняется known_subtotal.

- Неизвестный расход не ноль; pure calculation не читает experience/CRM.
- basis: hourly_rate или public_observations; coefficient/rounding берутся из policy, default1, finite positive. hourly_rate явный; цена включает только труд.
- public_observations требует matching operation_name, finite price_rub, labor_only=true и includes_parts=false. Неизвестный явный range_hours не заменяется hours.
- Duplicate/included operations исключаются; overlap требует явного overlap_resolved. No network/experience/CRM reads.

Подробный контракт: [справочник](../references/work-pricing.md). Карточка и inputSchema согласованы versioned export; ручной цвет не является результатом проверки.
