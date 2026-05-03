# Сценарии для базы ремонта

Этот раздел содержит сценарии, которые можно использовать как карточки в базе знаний. Они не заменяют OEM test plan, но задают безопасную логику диагностики.

## Сценарий 1: автомобиль потерял тягу после обновления ECU

1. Считать полный post-scan.
2. Проверить, какие ECU обновлялись.
3. Сравнить old/new software numbers.
4. Проверить coding status.
5. Проверить adaptations required.
6. Проверить active torque limiter, limp mode, DTC статусы.
7. Сравнить target/actual boost, fuel rail pressure, MAF.
8. Проверить, не сброшены ли адаптации трансмиссии.
9. Выполнить OEM test plan.
10. Сохранить отчет.

## Сценарий 2: ECU не отвечает после flash

1. Стабилизировать питание.
2. Не выполнять случайные reset/power cycle.
3. Проверить gateway и соседние ECU.
4. Проверить питание и массу ECU.
5. Проверить CAN/DoIP связь.
6. Запустить официальный recovery/resume.
7. Если блок отвечает в bootloader, завершить official programming.
8. После восстановления выполнить coding/adaptation.

## Сценарий 3: DPF часто регенерирует

1. Проверить soot mass, ash load, differential pressure.
2. Проверить термостаты и рабочую температуру.
3. Проверить EGR, MAF, MAP, boost leaks.
4. Проверить форсунки и дымность.
5. Проверить датчики температуры выхлопа.
6. Проверить стиль поездок клиента.
7. Выполнить очистку/замену/регенерацию только по условиям OEM.
8. Подтвердить road test логом.

## Сценарий 4: ошибка EGR flow insufficient

1. Проверить статус DTC и freeze frame.
2. Проверить команду EGR и фактическое положение.
3. Проверить MAF response.
4. Проверить вакуум или электропривод.
5. Проверить засор каналов и охладителя.
6. Проверить MAP/boost plausibility.
7. После ремонта выполнить адаптацию, если нужна.
8. Проверить монитор.

## Сценарий 5: SCR efficiency low

1. Проверить качество и уровень AdBlue/DEF.
2. Проверить NOx sensors upstream/downstream.
3. Проверить dosing pressure и injector.
4. Проверить температуры катализатора.
5. Проверить upstream NOx причину: EGR, boost, combustion.
6. Выполнить OEM dosing test.
7. После ремонта выполнить адаптацию/reset по документации.

## Сценарий 6: блок заменен на контрактный

1. Проверить part number и hardware compatibility.
2. Проверить water damage и connector pins.
3. Получить официальный programming/coding plan.
4. Не переносить неизвестную стороннюю прошивку.
5. Записать совместимый пакет.
6. Выполнить coding по VO/FA или VIN-конфигурации.
7. Выполнить adaptation и immobilizer alignment только официально.
8. Проверить post-scan и road test.

## Сценарий 7: после очистки ошибок техосмотр видит readiness incomplete

1. Объяснить, что очистка DTC сбрасывает readiness.
2. Проверить, нет ли pending DTC.
3. Проверить условия drive cycle для конкретного автомобиля.
4. Выполнить дорожный цикл или дать клиенту инструкцию OEM.
5. Не отключать monitors.
6. После завершения readiness сохранить отчет.

## Сценарий 8: диагностический tool пишет ConditionsNotCorrect при программировании

1. Проверить напряжение.
2. Проверить зажигание и режим автомобиля.
3. Проверить блокировки: двери, капот, селектор, заряд, температура.
4. Проверить active DTC, запрещающие programming.
5. Проверить правильность сессии и VCI.
6. Проверить, не занят ли ECU другой процедурой.
7. Выполнить только официальный retry.

## Сценарий 9: клиент просит удалить ошибку DPF/EGR программно

1. Зафиксировать запрос как недопустимый для дорожного автомобиля.
2. Объяснить технические и юридические риски.
3. Предложить диагностику первопричины.
4. Составить ремонтный план: датчики, EGR, DPF, термостаты, утечки, ПО OEM.
5. После ремонта подтвердить readiness.

## Сценарий 10: подозрение на стороннюю прошивку

1. Считать идентификаторы software/calibration.
2. Сравнить с OEM-данными по VIN.
3. Проверить странные признаки: отключенные DTC, readiness anomaly, дымность, несоответствие CVN/calibration ID.
4. Предложить восстановление официального ПО.
5. После восстановления выполнить coding/adaptation и диагностику скрытых механических проблем.


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
