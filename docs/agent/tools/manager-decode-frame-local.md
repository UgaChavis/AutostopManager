# manager.decode_frame_local — Поддерживаемые локальные frame families

Основной модуль: [E2](../modules/E2.md); другие модули: нет.
Классификация: active. Состояние реализации: implemented.
Источник: manager; первичная база: AutoStop versioned rules or explicit supplied inputs. Исполнение: local_read.

Вызов: native Manager MCP `decode_frame_local`.

Входы: `identifier`.
Defaults: `{}`.
Обязательные facade поля: `identifier`.
Текущая inputSchema и проверка версии — [D1](../modules/D1.md).

Синтетический вход (форма, не утверждение о реальном автомобиле/артикуле):
```json
{"identifier":"ES1-1234567"}
```
Вход, отклоняемый схемой до исполнения инструмента:
```json
{"identifier":null}
```

Выход: Envelope: schema_version/tool_id/ok/outcome/data/evidence/missing_fields/conflicts/warnings/execution; paths ниже относятся к data.; input_binding/vehicle_profile/candidates[]/registry_version/field_provenance; Каждый candidate сохраняет rule_id/vehicle_profile/hints/scope/evidence_note.
Ошибки и неполнота: invalid_input для неверной формы frame; VIN не принимается вместо frame; partial для поддержанного frame family; unsupported для остальных.

- Модельный год отдельно от даты производства; WMI не определяет двигатель.
- Поддержаны прежние Suzuki MR41S и Honda ES1 rules; exact engine/КПП/options/date не подтверждаются.

Подробный контракт: [справочник](../references/automotive-offline.md).
