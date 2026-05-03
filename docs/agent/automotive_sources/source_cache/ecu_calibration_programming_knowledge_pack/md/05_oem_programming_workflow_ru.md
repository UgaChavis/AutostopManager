# Законный workflow программирования ECU

Программирование ECU - управляемый сервисный процесс, а не простая запись файла. Главная задача - записать совместимый официальный пакет, не потерять связь, не нарушить кодирование и подтвердить результат.

## Когда программирование оправдано

- Производитель выпустил сервисную кампанию или бюллетень.
- Официальный test plan указывает на обновление ПО.
- Блок заменен и требует программирования/кодирования.
- Нужно восстановить заводское ПО после некорректной модификации.
- Требуется совместимость с замененной деталью по VIN.

Программирование не должно применяться как первая попытка ремонта без диагностики питания, массы, датчиков, механики и сети.

## Предварительная проверка

1. Идентифицировать автомобиль по VIN.
2. Считать полный pre-scan всех ECU.
3. Сохранить fault memory, freeze frame, current software numbers, hardware numbers, coding status.
4. Проверить АКБ и подключить стабилизированный источник питания.
5. Проверить состояние клеммы 15 и sleep/wake logic.
6. Убедиться, что нет активных сетевых проблем.
7. Проверить доступ к OEM-порталу и совместимость VCI.
8. Проверить, что файл или пакет получен из официального источника.
9. Проверить, что автомобиль физически готов: двери, свет, вентиляторы, заряд, подключение, ноутбук.

## Источник питания

Во время flash ECU может потреблять нестабильно, включать вентиляторы, насосы и шлюзы. Падение напряжения - одна из частых причин повреждения программирования. Используют service power supply, а не обычное зарядное устройство. Напряжение и ток выбирают по требованиям OEM.

## Общая цепочка программирования

```text
vehicle identification
pre-scan and report
compatibility check
programming plan / measures plan
power supply connected
download official package
enter programming session
erase target memory
transfer data blocks
verify checksum/signature
ECU reset
coding / variant configuration
adaptations / teach-in routines
clear faults only after repair
post-scan
road test and readiness check
final report
```

## Что происходит в протоколе

Внутри официальный инструмент может использовать UDS-сервисы: смена сессии, authorized security access, request download, transfer data, routine control для проверки, reset. Пользователь сервиса не должен вручную обходить security access или подменять пакеты. Наличие стандартных сервисов не означает, что можно записывать произвольный файл.

## Совместимость пакета

Пакет должен подходить по VIN и модели, hardware number, bootloader compatibility, software part number, calibration ID, emission certification/market, transmission and drivetrain, gateway and integration level, dependency with other ECU. Современное программирование часто обновляет не один блок, а группу блоков, чтобы сохранить совместимость.

## Coding после программирования

После записи application/calibration ECU может быть пустым или иметь default coding. Coding задает рынок, сторону руля, тип трансмиссии, комплектацию, наличие датчиков, варианты света, тормозов, ассистентов, режимы связи с соседними ECU. Неправильный coding может дать DTC без физической неисправности.

## Adaptation после программирования

Примеры адаптаций: дроссельная заслонка, VANOS/Valvetronic у BMW, EGR position, турбина/VGT, DPF differential pressure sensor zeroing, форсунки, transmission clutch fill times, steering angle sensor, battery registration, ride height calibration. Адаптацию выполняют только по OEM-процедуре и при выполненных условиях.

## Очистка ошибок

Очистка DTC до ремонта стирает контекст. Правильная последовательность: сохранить DTC и freeze frame, выполнить диагностику и ремонт, выполнить программирование/кодирование/адаптацию, очистить ошибки, воспроизвести условия монитора, подтвердить, что DTC не возвращается.

## Документирование

Каждое программирование должно иметь отчет: дата, VIN, пробег, причина программирования, источник пакета, старые и новые software numbers, состояние АКБ, VCI и версия ПО, pre-scan, post-scan, выполненные адаптации, результат road test.

## Признаки, что надо остановиться

- Нестабильное напряжение.
- Потеря связи с gateway.
- Несовпадение hardware number.
- Tool предлагает неожиданный downgrade.
- Активные ошибки питания, CAN или иммобилайзера.
- Неясная история сторонней прошивки.
- Нет доступа к официальному recovery plan.


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
