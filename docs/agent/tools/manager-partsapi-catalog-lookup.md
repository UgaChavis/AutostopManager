# manager.partsapi_catalog_lookup — Один выбранный PartsAPI метод

Один выбранный PartsAPI метод

Основной модуль: [E1](../modules/E1.md); другие модули: нет.
Классификация: active. Состояние реализации: implemented.
Источник: partsapi_ru; первичная база: declared_by_provider; VIN/TecDoc/maintenance/AUTONORMS namespaces. Исполнение: network_read.

Вызов: native Manager MCP `partsapi_catalog_lookup`.

Входы: `operation`, `identifier`, `registration_number`, `part_number`, `article_id`, `supplier_id`, `provider_parameters`, `brand`, `category`, `vehicle_type`, `type_id`, `lang`, `lang_id`, `make_name_seo`, `model_id`, `motor_id`, `top_category_id`, `sub_category_id`, `car_id`, `timeout`, `max_attempts`, `dry_run`, `catalog_context`, `detail`.
Defaults: `{"identifier":null,"registration_number":null,"part_number":null,"article_id":null,"supplier_id":null,"provider_parameters":null,"brand":null,"category":null,"vehicle_type":null,"type_id":null,"lang":null,"lang_id":null,"make_name_seo":null,"model_id":null,"motor_id":null,"top_category_id":null,"sub_category_id":null,"car_id":null,"timeout":20.0,"max_attempts":1,"dry_run":false,"catalog_context":null,"detail":"full"}`.
Обязательные facade поля: `operation`.
Fingerprint проверяет [manifest](../manager_mcp_catalog.json); полная inputSchema берётся из регистрации и `tools/list`, в CRM — native_schemas того же bundle.

Синтетический вход (форма, не утверждение о реальном автомобиле/артикуле):
```json
{"operation":"search_articles","part_number":"DEMO-ARTICLE-001","dry_run":true,"detail":"summary"}
```
Вход, отклоняемый схемой до исполнения инструмента:
```json
{"operation":[]}
```

Выход: Legacy flat adapter: ok/provider/operation/partsapi_method/docs_url/role/quota_cost_estimate/request_plan/privacy/catalog_binding.; outcome/failure_class/retryable/requires_fallback/attempt_count/max_attempts/attempts/payload/response_shape/empty_payload/record_counts.; vehicle_profiles/oem_candidates/cross_candidates/article_candidates/autonorms_rows/fill_volumes/search_tree_rows/article_criteria_rows; sensitive identifiers are redacted.; Summary retains normalized evidence and execution measurements, limits collection previews to25 and reports total/returned/truncated; raw payload is not duplicated..
Ошибки и неполнота: outcome includes invalid_operation/invalid_input/credentials_missing/configured_unverified/empty_result/unparsed_response/success/identifier_mismatch/identifier_unverified; failure_class preserves provider_auth_error/provider_ip_quota_exceeded/provider_rejected/transport/parse failures. Dry run attempt_count=0 is not authenticated success..

- Selected methods validate actual provider IDs/category context before HTTP; supplied typed modification/node refs must match namespace, carType and exact tree hash.
- Legacy raw parameters remain compatible but catalog_binding=raw_parameters_unverified and cannot establish an exact category or fitment.
- TecDoc article, cross or OE-reference rows are not original VIN-specific OEM evidence; paid/quota requests require an explicitly selected live branch.
- type_id is a string, for example "42". Full normalization and conflict checks run before summary truncation; detail=full remains the compatible default.

Подробный контракт: [справочник](../references/manager-runtime.md). Карточка и inputSchema согласованы versioned export; ручной цвет не является результатом проверки.
