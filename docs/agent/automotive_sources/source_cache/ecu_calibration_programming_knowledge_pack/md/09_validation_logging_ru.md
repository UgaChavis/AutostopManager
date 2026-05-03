# Валидация, логи и контроль качества ремонта

Программирование или ремонт экологической системы завершены только после проверки результата. Отсутствие активной ошибки сразу после очистки DTC - недостаточное доказательство. Нужны измерения, test plan, road test и документирование.

## Что фиксировать до ремонта

- VIN, пробег, жалоба клиента.
- Все DTC со статусами.
- Freeze frame.
- Идентификаторы ECU: hardware, software, calibration ID.
- Live data в режиме проявления симптома.
- Условия: температура, скорость, нагрузка, топливо, высота, окружающая температура.
- История предыдущих ремонтов и сторонних программных изменений.

## Что фиксировать после ремонта

- Какие компоненты заменены или отремонтированы.
- Какие адаптации выполнены.
- Какие ECU запрограммированы/закодированы.
- Новые software numbers.
- Post-scan.
- Readiness status.
- Лог road test.
- Остаточные DTC, если есть, и их статус.

## Минимальный набор логов для дизеля с DPF/EGR/SCR

- engine speed;
- vehicle speed;
- coolant temperature;
- oil temperature, если доступно;
- MAF actual;
- MAP/boost actual and target;
- EGR commanded and actual;
- DPF differential pressure;
- calculated soot mass;
- ash load;
- exhaust temperature sensors;
- NOx upstream/downstream;
- SCR dosing command;
- fuel rail pressure actual/target;
- injector correction, если доступно;
- battery voltage.

## Минимальный набор логов для бензинового двигателя

- engine speed;
- load;
- coolant temperature;
- intake air temperature;
- MAF/MAP;
- lambda/O2 upstream and downstream;
- short term fuel trim;
- long term fuel trim;
- misfire counters;
- VANOS/Valvetronic target and actual, если применимо;
- fuel pressure;
- catalyst temperature estimate, если доступно;
- battery voltage.

## Принципы анализа логов

- Сравнивать target и actual.
- Проверять физическую правдоподобность значений.
- Учитывать задержку датчиков и фильтры.
- Анализировать события до DTC, а не только после.
- Сравнивать режимы: idle, part load, acceleration, deceleration, steady cruise.
- Не делать вывод по одному PID без контекста.

## Пример анализа EGR

```text
Команда EGR открыта, actual position открывается.
MAF должен уменьшиться, потому что часть свежего воздуха заменена EGR.
Если actual открыт, но MAF не меняется: возможна механическая блокировка потока.
Если actual не следует команде: привод, питание, загрязнение, адаптация.
Если MAF меняется слишком сильно: клапан зависает, ошибка модели, утечка.
```

## Пример анализа DPF differential pressure

```text
Idle pressure слишком высокое: засор DPF, шланги, датчик, конденсат.
Pressure растет резко с rpm: ограничение потока или неправильные шланги.
Calculated soot низкая, pressure высокое: зола, датчик, шланги, механическое ограничение.
Calculated soot высокая, pressure умеренное: проверить модель, датчики, историю регенераций.
```

## Readiness и drive cycle

Readiness monitor должен завершиться в условиях, заданных производителем. После очистки ошибок часть мониторов будет incomplete. Для выдачи автомобиля клиенту после экологического ремонта надо объяснить, что readiness может потребовать drive cycle, но DTC не должен возвращаться при выполнении условий.

## Acceptance criteria

Ремонт можно считать подтвержденным, если нет active DTC, post-scan сохранен, live data правдоподобны, target/actual сходятся в допустимом диапазоне, адаптации завершены, readiness выполнены или имеют объяснимый статус, жалоба клиента не воспроизведена на road test, нет новых сетевых ошибок после sleep/wake.

## Контроль версий знаний

При загрузке этого комплекта в базу знаний важно хранить дату создания, источник и область применения. Стандарты, OEM-процедуры и законодательство меняются. Любой конкретный момент затяжки, температура, уровень масла, версия ПО или процедура flash должны проверяться по актуальной документации для VIN.


## Опорные публичные источники

- ASAM MCD-2 MC (ASAP2/A2L): https://www.asam.net/standards/detail/mcd-2-mc/
- ASAM MCD-2 D (ODX): https://www.asam.net/standards/detail/mcd-2-d/
- ASAM MDF: https://www.asam.net/standards/detail/mdf/
- ASAM CDF: https://www.asam.net/standards/detail/cdf/
- ISO 14229-1:2020 Unified Diagnostic Services: https://www.iso.org/standard/72439.html
- ISO 15765-2 Diagnostic communication over CAN (DoCAN): https://www.iso.org/standard/94385.html
- ISO 13400-2 Diagnostics over Internet Protocol (DoIP): https://www.iso.org/standard/74785.html
- SAE J2534 Pass-Thru Vehicle Programming: https://www.sae.org/standards/j2534-1_5_00-recommended-practice-pass-thru-vehicle-programming
- AUTOSAR Classic Platform: https://www.autosar.org/standards/classic-platform
