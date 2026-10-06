# extension.frame_registry — Расширенный реестр frame

Roadmap: внешний лицензированный источник с проверяемым контрактом

Основной модуль: E2; ссылки: нет. Состояние реализации: planned.
Источник: manager; первичная база: AutoStop versioned rules or explicit supplied inputs. Исполнение: pure.

Рабочий invocation пока отсутствует. Внешняя база/лицензия/доступ должны быть отдельно подготовлены; ссылка не является API.

Выход: outcome/data/evidence/missing_fields/conflicts/warnings/execution (новые helpers); legacy поля сохраняются.
Ошибки и неполнота: invalid_input сохраняет ошибочный вход как отказ; empty/partial/unsupported не success; configuration/auth/quota/provider/parse ошибки различаются для сетевого источника.

- Источник ещё не подключён; не выполнять выдуманный invocation.
- Лицензия/credentials/покрытие — внешняя activation dependency, покупка не входит в текущую задачу.

Подробный контракт: [справочник](../references/vehicle-identity.md). Карточка и inputSchema согласованы versioned export; ручной цвет не является результатом проверки.
