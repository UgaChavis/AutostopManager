# manager.resolve_catalog_group — Узлы готового связанного дерева

Узлы готового связанного дерева

Основной модуль: [E4](../modules/E4.md); другие модули: нет.
Классификация: active. Состояние реализации: implemented.
Источник: manager; первичная база: AutoStop versioned rules or explicit supplied inputs. Исполнение: pure.

Вызов: native Manager MCP `resolve_catalog_group`.

Входы: `tree`, `intent`, `modification`, `selected_node_id`.
Defaults: `{"selected_node_id":null}`.
Обязательные facade поля: `tree`, `intent`, `modification`.
Fingerprint проверяет [manifest](../manager_mcp_catalog.json); полная inputSchema берётся из регистрации и `tools/list`, в CRM — native_schemas того же bundle.

Синтетический вход (форма, не утверждение о реальном автомобиле/артикуле):
```json
{"intent":"амортизатор","modification":{"carType":"PC","entity_kind":"modification","id":"42","namespace":"tecdoc","provider":"partsapi_ru"},"tree":{"modification":{"carType":"PC","entity_kind":"modification","id":"42","namespace":"tecdoc","provider":"partsapi_ru"},"rows":[{"NODE_3_STR_ID":"18","NODE_3_TEXT":"Амортизатор"}]}}
```
Вход, отклоняемый схемой до исполнения инструмента:
```json
{"intent":"передние колодки","modification":{"carType":"PC","entity_kind":"modification","id":"DEMO-CAR","namespace":"tecdoc","provider":"partsapi_ru"},"tree":null}
```

Выход: Envelope: schema_version/tool_id/ok/outcome/data/evidence/missing_fields/conflicts/warnings/execution; paths ниже относятся к data.; category/category_kind/category_mode/category_queryable/category_unresolved/candidate_node_ids/nodes/tree_sha256 и остальные поля разрешения группы; nodes[]: provider/namespace/entity_kind/carType/id/parent/tree_sha256 для последующего direct getArticles catalog_context.
Ошибки и неполнота: invalid_input при невалидном TecDoc reference, чужом tree.modification или malformed rows; partial при неразрешённой/неоднозначной группе; success только для queryable leaf.

- Article/cross/OE-reference не подтверждают оригинальный номер конкретного VIN.
- modification: provider=partsapi_ru, namespace=tecdoc, entity_kind=modification, положительный numeric id, carType PC/CV/Motorcycle. tree.modification должен точно совпасть.
- Не смешивать tree nodes разных модификаций; queryable group не подтверждает OEM/fitment.

Подробный контракт: [справочник](../references/partsapi.md). Карточка и inputSchema согласованы versioned export; ручной цвет не является результатом проверки.
