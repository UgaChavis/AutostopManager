# BMW: обзор диагностики, программирования и кодирования

BMW использует официальные сервисные платформы для диагностики, ремонта, кодирования и программирования. Для независимого сервиса правильный путь - официальный доступ через BMW AOS/TIS/AIR и совместимое оборудование, а не случайные пакеты из форумов.

## Основные термины BMW

- AOS (Aftersales Online System - послепродажная онлайн-система): официальный портал BMW Group для независимых мастерских.
- AIR (Aftersales Information Research - послепродажная информационная система): техническая информация, ремонтные инструкции, электросхемы, данные по автомобилю.
- ISTA (Integrated Service Technical Application - интегрированное сервисно-техническое приложение): диагностика, test plans, программирование и сервисные функции.
- ICOM (Integrated Communication Optical Module - интегрированный коммуникационный модуль): официальный интерфейс связи BMW.
- ENET (Ethernet to OBD interface - Ethernet-интерфейс к диагностическому разъему): используется на ряде платформ для Ethernet/DoIP задач.
- I-level / Integration level: уровень интеграции ПО автомобиля.
- Measures plan: план мероприятий программирования и кодирования.
- VO/FA (Vehicle Order / Fahrzeugauftrag - заказ/конфигурация автомобиля): набор опций и параметров комплектации.
- TAL (Transaction Action List - список действий транзакции): инженерный термин плана операций на блоках.

## Coding vs programming в BMW

Programming меняет программное обеспечение или калибровки блока. Coding записывает параметры комплектации и вариантное кодирование. После замены блока часто нужны оба этапа: сначала записать совместимое ПО, затем закодировать блок под VO/FA и выполнить адаптации.

## Типовые ECU BMW

- DME (Digital Motor Electronics - цифровая электроника двигателя): бензиновый двигатель.
- DDE (Digital Diesel Electronics - цифровая дизельная электроника): дизельный двигатель.
- EGS (Electronic Gearbox System - электронная система коробки передач): автоматическая коробка.
- DSC (Dynamic Stability Control - динамический контроль устойчивости): тормоза, ABS, стабилизация.
- EPS (Electric Power Steering - электрический усилитель рулевого управления).
- VTG (Verteilergetriebe - раздаточная коробка): xDrive.
- CAS (Car Access System - система доступа автомобиля), EWS (Elektronische Wegfahrsperre - электронная противоугонная система), FEM (Front Electronic Module - передний электронный модуль), BDC (Body Domain Controller - контроллер домена кузова).
- ZGM/ZGW (Central Gateway Module - центральный шлюз): маршрутизация сетей.
- KOMBI (Instrument cluster - комбинация приборов).
- HU (Head Unit - головное устройство).
- FRM (Footwell Module - модуль пространства ног), JBE (Junction Box Electronics - электроника распределительной коробки).
- EKPS (Electric Fuel Pump Control - электронное управление топливным насосом).

## Предварительный BMW-check

Перед программированием BMW:

1. VIN распознан корректно.
2. ISTA видит все ожидаемые ECU.
3. Нет активных ошибок питания, gateway, immobilizer или Ethernet.
4. АКБ исправна, power supply подключен.
5. I-level и measures plan получены из официального источника.
6. Нет нерешенных механических проблем, которые могут сорвать test plan.
7. Клиент предупрежден о времени, рисках и необходимости последующей адаптации.

## Что важно в BMW после программирования

- Проверить sleep/wake: некоторые ошибки проявляются после засыпания автомобиля.
- Проверить battery registration, если работы касались АКБ.
- Выполнить адаптации двигателя: Valvetronic, VANOS, дроссель, EGR, DPF sensor, если ISTA требует.
- Проверить EGS адаптации и связь с DME/DDE.
- Проверить DSC/steering angle sensor при работах по шасси.
- Проверить VTG calibration при работах с xDrive, шинами, раздаточной коробкой или маслом.
- Сохранить pre-scan и post-scan.

## Типовые ошибки после замены блока

- Блок физически совместим, но имеет другое аппаратное семейство.
- Блок от другой модели имеет иной bootloader.
- VIN в блоке не соответствует автомобилю.
- Coding default, поэтому ECU ожидает отсутствующие датчики.
- Не выполнена синхронизация с иммобилайзером через официальный процесс.
- Не выполнена адаптация исполнительного механизма.
- Уровень интеграции соседних блоков не совместим.

## BMW и сторонние инструменты

Инженерские или неофициальные инструменты могут показывать больше низкоуровневых данных, но их неправильное применение повышает риск. Для автосервиса при ремонте предпочтительны официальные test plans и authorized programming. Нелицензионные пакеты и непроверенные данные создают риски повреждения ECU, нарушения экологической сертификации и потери возможности официального recovery.

## BMW diesel emissions: ремонтная логика

При жалобах на DPF, EGR, NOx или SCR:

- считать DDE fault memory и freeze frames;
- проверить soot mass, ash mass, differential pressure, exhaust temperatures;
- проверить EGR command/actual и MAF plausibility;
- проверить термостаты и способность двигателя достигать рабочей температуры;
- проверить свечи накала и блок управления свечами, если регенерация зависит от них;
- проверить качество/уровень AdBlue и NOx sensors для SCR;
- выполнять forced regeneration только по ISTA и только после проверки условий;
- не удалять и не отключать системы контроля выбросов.

## BMW gasoline emissions: ремонтная логика

Для бензиновых BMW важны: lambda control и fuel trims, VANOS/Valvetronic adaptations, катализатор и O2 sensors, EVAP (Evaporative Emission Control System - система улавливания паров топлива), misfire counters, fuel pressure и air leaks, software update только по test plan или бюллетеню.

## Минимальный отчет для BMW

```text
VIN:
Model / engine / gearbox:
Mileage:
Customer complaint:
ISTA version / source:
VCI:
Power supply:
Pre-scan saved: yes/no
Old I-level:
New I-level:
Measures plan summary:
ECUs programmed:
ECUs coded:
Adaptations performed:
Post-scan result:
Road test result:
Remaining faults:
```

## Практическое правило

Для BMW программирование без стабильного питания, официального плана и сохраненного pre-scan - высокий риск. Для BMW с жалобой на экологические системы программирование не заменяет диагностику механики, датчиков, термостатов, утечек и качества расходников.


## Опорные публичные источники

- BMW AOS: https://aos.bmwgroup.com/
- BMW AOS price list: https://aos.bmwgroup.com/price-list
- ISO 14229 UDS: https://www.iso.org/standard/72439.html
- SAE J2534: https://www.sae.org/standards/j2534-1_5_00-recommended-practice-pass-thru-vehicle-programming
