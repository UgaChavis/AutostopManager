# extension.dtc — Процедуры по DTC

Roadmap: внешний лицензированный источник с проверяемым контрактом

Основной модуль: [E14](../modules/E14.md); другие модули: нет.
Классификация: active. Состояние реализации: planned.
Источник: manager; первичная база: AutoStop versioned rules or explicit supplied inputs. Исполнение: pure.

Рабочий invocation пока отсутствует. Внешняя база/лицензия/доступ должны быть отдельно подготовлены; ссылка не является API.

Выход: outcome/data/evidence/missing_fields/conflicts/warnings/execution (новые helpers); legacy поля сохраняются.
Ошибки и неполнота: invalid_input сохраняет ошибочный вход как отказ; empty/partial/unsupported не success; configuration/auth/quota/provider/parse ошибки различаются для сетевого источника.

- Источник ещё не подключён; не выполнять выдуманный invocation.
- Лицензия/credentials/покрытие — внешняя activation dependency, покупка не входит в текущую задачу.

Подробный контракт: [справочник](../references/automotive-sources.md).
