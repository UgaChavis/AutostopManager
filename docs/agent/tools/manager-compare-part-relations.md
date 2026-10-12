# manager.compare_part_relations — Типы связей и конфликты номеров

Основной модуль: [E5](../modules/E5.md); другие модули: нет.
Классификация: active. Состояние реализации: implemented.
Источник: manager; первичная база: AutoStop versioned rules or explicit supplied inputs. Исполнение: pure.

Вызов: native Manager MCP `compare_part_relations`.

Входы: `relations`.
Defaults: `{}`.
Обязательные facade поля: `relations`.
Текущая inputSchema и проверка версии — [D1](../modules/D1.md).

Синтетический вход (форма, не утверждение о реальном автомобиле/артикуле):
```json
{"relations":[{"evidence":[{"declares_oem":true,"document_kind":"official_epc","fetched_at":"2026-01-01T00:00:00Z","locator":"https://example.com/oem","method":"document_read","primary_lineage":"DEMO official OEM document","provider":"manufacturer","scope":"family","version":"demo-v1"}],"from":{"brand":"DEMO","number":"DEMO-OLD"},"to":{"brand":"DEMO","number":"DEMO-NEW"},"type":"cross"}]}
```
Вход, отклоняемый схемой до исполнения инструмента:
```json
{"relations":null}
```

Выход: Envelope: schema_version/tool_id/ok/outcome/data/evidence/missing_fields/conflicts/warnings/execution; paths ниже относятся к data.; relations[]: нормализованные from/to numbers, type/direction/qualifiers/evidence, fitment_confirmed=false; conflicts сохраняет supersession branches.
Ошибки и неполнота: invalid_input для endpoints/evidence неверной формы или >500; partial/unverified_supersession если direction/primary source/endpoints не подтверждены.

- Cross/analog/oe_reference/supersession различны; замена требует направления и первичного свидетельства.
- type: cross/analog/oe_reference/supersession различны.
- Supersession требует direction from_to/to_from и matching source.from/source.to number+brand+direction, primary document manufacturer_supersession/official_epc и полные source metadata с aware ISO fetched_at.

Подробный контракт: [справочник](../references/partsapi.md).
