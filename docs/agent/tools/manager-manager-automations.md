# manager.manager_automations — manager_automations

Справочный/отдельный контур; не automotive workflow

Основной модуль: outside; другие модули: нет.
Классификация: outside. Состояние реализации: implemented.
Источник: manager; первичная база: Manager Automation Center Unix control socket; scheduler registry metadata and timer state. Исполнение: composed.

Вызов: native Manager MCP `manager_automations`.

Входы: `operation`, `job_id`, `include_archived`.
Defaults: `{"include_archived":false,"job_id":"","operation":"status"}`.
Обязательные facade поля: нет; ограничения конкретной операции всё равно применяются.
Текущая inputSchema и проверка версии — [D1](../modules/D1.md).

Синтетический вход (форма, не утверждение о реальном автомобиле/артикуле):
```json
{}
```
Вход, отклоняемый схемой до исполнения инструмента:
```json
{"operation":"__invalid__"}
```

Выход: Успех: ok:true и данные операции на верхнем уровне; общий tool_execution содержит техническую телеметрию вызова.; status/default: generated_at, can_manage, schema_version, controller, global_hold, jobs, system_timers, readiness, templates.; templates: templates; readiness: ready, checks, warnings, execution_packet..
Ошибки и неполнота: Отказ: ok:false и error — строковый код controller; могут добавляться job_id, revision, expected_revision, current_revision.; automation_control_unavailable при ошибке транспорта; automation_control_failed при отказе без кода controller.; Ошибка job/schema/role возвращается отдельным кодом, например automation_job_not_found или automation_schema_newer_than_runtime..

- Назначение и полномочия определяет профильный контур; automotive smoke не выполняет записи.
- Наличие в общем native inventory не разрешает business action или чтение клиента.
- Default/status/readiness могут инициализировать registry и обновить schema metadata текущей схемы.
- Миграция старой схемы добавляет/заполняет технические поля jobs/runtime/controller, включая schedule_timezone и applied_at.
- Status/readiness не меняют desired_state/revision заданий, не выполняют guarded действия, не ставят сообщения в очередь и не отправляют их.
- templates возвращает allowlisted шаблоны без доступа к registry и без записи.

Подробный контракт: [справочник](../references/manager-runtime.md).
