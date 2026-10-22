# manager.manager_automation_control — manager_automation_control

Справочный/отдельный контур; не automotive workflow

Основной модуль: outside; другие модули: нет.
Классификация: outside. Состояние реализации: implemented.
Источник: manager; первичная база: Manager Automation Center Unix control socket; scheduler registry, allowlisted templates and system timer state. Исполнение: business_write.

Вызов: native Manager MCP `manager_automation_control`.

Входы: `operation`, `payload`, `idempotency_key`, `expected_revision`.
Defaults: `{"expected_revision":null,"idempotency_key":"","payload":null}`.
Обязательные facade поля: `operation`.
Текущая inputSchema и проверка версии — [D1](../modules/D1.md).

Синтетический вход (форма, не утверждение о реальном автомобиле/артикуле):
```json
{"operation":"preview","payload":{"target_operation":"create_from_template","target_payload":{"template_id":"crm_digest_v1"}}}
```
Вход, отклоняемый схемой до исполнения инструмента:
```json
{"operation":"__invalid__"}
```

Выход: Успех: ok:true и данные операции на верхнем уровне; общий tool_execution содержит техническую телеметрию вызова.; preview: target_operation, would_change, proposed, external_effects; current для существующего job/timer, allowed/locked для timer.; Guarded apply возвращает created/changed/queued/archived и поля конкретной операции: job, system_timer, run_id, outbox_id либо job_id/revision; повтор может включать idempotent_replay..
Ошибки и неполнота: Отказ: ok:false и error — строковый код controller; при конфликте могут добавляться job_id, revision, expected_revision, current_revision.; automation_control_unavailable при ошибке транспорта; automation_control_failed при отказе без кода controller.; Payload/template/job/schedule/permission отказы сохраняют код controller; изменения требуют idempotency key и текущую revision для существующего job.; automation_revision_conflict, idempotency_key_invalid или idempotency_key_conflict требуют исправить вход и перечитать текущий объект..

- Назначение и полномочия определяет профильный контур; automotive smoke не выполняет записи.
- Наличие в общем native inventory не разрешает business action или чтение клиента.
- Preview существующего job может создавать, обновлять или восстанавливать техническую схему registry.
- Миграция старой схемы добавляет/заполняет технические поля jobs/runtime/controller, включая schedule_timezone и applied_at.
- upgraded_at фиксирует создание записи компонента или смену версии; repair текущей версии сохраняет эту метку. Полностью актуальный registry повторный preview существующего job не меняет.
- Preview не выполняет предлагаемое guarded действие, не меняет desired_state/revision заданий, не ставит сообщения в очередь и не отправляет их; preview создания из template не обращается к registry.
- Whole-tool idempotentHint=false conservatively covers initialization, version upgrades and repairs. Fully current repeated existing-job previews preserve registry state. Guarded mutation idempotency keys, exact-revision CAS, stored receipts and controller permissions retain their existing guarantees.

Подробный контракт: [справочник](../references/manager-runtime.md).
