# Назначение и границы комплекта

Этот комплект предназначен для обучения диагностике, пониманию калибровок, безопасному программированию блоков управления и восстановлению автомобилей после ошибок программирования. Материал ориентирован на автосервис, который работает с серийными автомобилями и обязан сохранять заводскую безопасность, экологическую сертификацию и юридическую пригодность автомобиля к эксплуатации.

## Что входит

- Базовая архитектура ECU (Electronic Control Unit - электронный блок управления): микроконтроллер, питание, входы, выходы, CAN (Controller Area Network - сеть контроллеров), flash-память, EEPROM (Electrically Erasable Programmable Read-Only Memory - электрически стираемая программируемая постоянная память), RAM (Random Access Memory - оперативная память).
- Разница между firmware (прошивка), calibration (калибровка), coding (кодирование по комплектации), adaptation (адаптация) и configuration (конфигурация).
- Принципы UDS (Unified Diagnostic Services - унифицированные диагностические сервисы), OBD II (On-Board Diagnostics II - бортовая диагностика второго поколения), ISO-TP (ISO Transport Protocol - транспортный протокол ISO поверх CAN), DoIP (Diagnostics over Internet Protocol - диагностика по интернет-протоколу).
- Как выглядят учебные файлы A2L (ASAM MCD-2 MC), ODX (Open Diagnostic Data Exchange), Intel HEX, Motorola S-record, DCM (Data Calibration Management), CDF (Calibration Data Format), MDF (Measurement Data Format).
- Законный процесс обновления ECU через OEM (Original Equipment Manufacturer - производитель оригинального оборудования) или разрешенный J2534 (SAE pass-thru) процесс.
- Диагностика EGR, DPF, SCR (Selective Catalytic Reduction - селективное каталитическое восстановление), NOx (Nitrogen Oxides - оксиды азота), GPF (Gasoline Particulate Filter - бензиновый сажевый фильтр) без отключения этих систем.

## Что не входит

- Инструкции по отключению EGR, DPF, SCR, катализаторов, NOx-датчиков, AdBlue, readiness monitors или MIL (Malfunction Indicator Lamp - контрольная лампа неисправности).
- Инструкции по созданию defeat device (устройство или программное изменение для обхода экологического контроля).
- Поиск и патчинг DTC (Diagnostic Trouble Code - диагностический код неисправности) switch, monitor switch, torque limiter delete, smoke limiter bypass, limp mode delete, checksum bypass или signature bypass.
- Методы обхода Seed-Key, SecurityAccess, иммобилайзера, цифровой подписи, защиты загрузчика, криптографической аутентификации ECU.
- Схемы нелицензионной загрузки заводского ПО или закрытых калибровочных пакетов.

## Почему это ограничено

Современный автомобиль - сертифицированная система. Экологические системы, тормоза, рулевое управление, силовая установка и высоковольтные системы связаны программно. Некорректное изменение калибровки может привести к превышению выбросов, повреждению двигателя, отказу тормозной или трансмиссионной логики, пожару, невозможности пройти техосмотр или юридической ответственности сервиса.

## Разделение задач в сервисе

1. Диагностика: найти физическую, электрическую или программную причину неисправности.
2. Ремонт: восстановить работоспособность компонентов согласно документации производителя.
3. Официальное обновление: установить разрешенный пакет ПО, если производитель выпустил обновление, кампанию или сервисный бюллетень.
4. Кодирование: привести конфигурацию блока к фактической комплектации автомобиля.
5. Адаптация: выполнить обучающие процедуры после замены деталей, ремонта или программирования.
6. Валидация: подтвердить отсутствие ошибок, корректность live data, readiness, пробную поездку и сохраненный отчет.

## Базовый словарь

- ECU - Electronic Control Unit - электронный блок управления.
- MCU - Microcontroller Unit - микроконтроллер.
- Bootloader - загрузчик, минимальная программа для проверки, записи и запуска основного ПО.
- Application - основная программа ECU.
- Calibration - набор калибровочных значений, таблиц, кривых и карт, влияющих на алгоритмы.
- Coding - конфигурация ECU под комплектацию автомобиля, обычно без изменения алгоритма управления.
- Adaptation - обученное значение, которое ECU накапливает после эксплуатации или сервисной процедуры.
- Variant coding - вариантное кодирование, выбор функционального варианта блока под модель, рынок, двигатель, трансмиссию и комплектацию.
- Flashing - запись ПО или калибровки во flash-память ECU.
- Checksum - контрольная сумма для обнаружения ошибок данных.
- Digital signature - цифровая подпись для проверки подлинности пакета.
- DTC - Diagnostic Trouble Code - диагностический код неисправности.
- Readiness - готовность OBD-мониторов экологических систем.

## Практический принцип

Для автосервиса рабочая цель - не изменить автомобиль так, чтобы он перестал контролировать неисправность, а найти первопричину, восстановить заводскую логику и подтвердить результат измерениями. Любой файл, который предполагается записывать в автомобиль, должен быть проверен по VIN (Vehicle Identification Number - идентификационный номер автомобиля), номеру блока, номеру ПО, аппаратной версии, рынку, трансмиссии, типу двигателя и официальному источнику.


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
- BMW Aftersales Online System: https://aos.bmwgroup.com/
- BMW AOS price list: https://aos.bmwgroup.com/price-list
- EPA - Tampering and Aftermarket Defeat Devices: https://www.epa.gov/enforcement/aftermarket-defeat-devices-and-tampering-are-illegal-and-undermine-vehicle-emissions
- CARB - Defeat devices warning: https://ww2.arb.ca.gov/news/carb-warns-vehicle-and-engine-manufacturers-about-hiding-software-or-hardware-changes-affect
