# BMW: коды неисправностей, OBD II и BMW fault memory

**Кратко:** Машиночитаемая база по структуре DTC, типовым OBD II кодам и публичным примерам BMW fault memory.

Дата сборки: 2026-05-03T10:32:46Z

## 1. Что такое DTC

DTC (Diagnostic Trouble Code — диагностический код неисправности) — это код, который блок управления записывает при обнаружении условия, соответствующего диагностическому критерию. В OBD II (On-Board Diagnostics II — бортовая диагностика второго поколения) различают pending fault code (ожидающий код), confirmed fault code (подтвержденный код), permanent fault code (постоянный код) и freeze frame (кадр параметров на момент фиксации).

SAE J2012 (Society of Automotive Engineers J2012 — стандарт диагностических кодов) задает формат стандартизированных DTC и диапазоны, зарезервированные для производителей. В BMW одновременно встречаются стандартные OBD II P-коды, расширенные BMW-коды в шестнадцатеричной форме и текстовые записи ISTA.

## 2. Формат OBD II

| Позиция | Значение | Пример |
| --- | --- | --- |
| 1-й символ | Система: P = Powertrain (силовой агрегат), C = Chassis (шасси), B = Body (кузов), U = Network (сеть). | P0300, U0100 |
| 2-й символ | 0 = стандартизированный код, 1 = код производителя, 2/3 = дополнительные стандартизированные или производственные диапазоны в зависимости от системы. | P0171, P1520 |
| 3-й символ у P-кодов | Подсистема: 1/2 топливо/воздух, 3 зажигание/пропуски, 4 выбросы, 5 скорость/холостой ход, 6 электроника, 7/8 коробка передач. | P0301, P0741 |
| 4-5-й символы | Конкретная неисправность внутри подсистемы. | P0171 = бедная смесь банк 1 |

## 3. Высокоценные общие OBD II P-коды для BMW

| Код | Стандартизированное значение | BMW-контекст диагностики |
| --- | --- | --- |
| P0100-P0104 | MAF (Mass Air Flow — массовый расход воздуха) circuit/range | Подсос воздуха, загрязнение MAF, проводка, питание датчика, неверные коррекции топлива. |
| P0112-P0113 | IAT (Intake Air Temperature — температура впуска) low/high | Датчик температуры впуска в корпусе MAF или TMAP (Temperature and Manifold Absolute Pressure — температура и абсолютное давление во впуске). |
| P0128 | Coolant thermostat below regulating temperature | Термостат, температура охлаждающей жидкости, электрический термостат на N/B-сериях. |
| P0171/P0174 | System too lean bank 1 / bank 2 | Подсос воздуха после MAF, PCV (Positive Crankcase Ventilation — вентиляция картера), трещины впуска, низкое давление топлива. |
| P0172/P0175 | System too rich bank 1 / bank 2 | Форсунки, давление топлива, MAF, датчики кислорода, масло/топливо во впуске. |
| P0300 | Random/multiple cylinder misfire | Связать с цилиндровыми счетчиками, топливными коррекциями, компрессией и давлением топлива. |
| P0301-P0308 | Misfire cylinder 1-8 | Катушка, свеча, форсунка, компрессия, подсос около цилиндра, механика клапанов. |
| P0420/P0430 | Catalyst efficiency below threshold bank 1 / bank 2 | Катализатор, датчики кислорода, подсос/пропуски, неверное топливо. |
| P0440-P0456 | EVAP (Evaporative Emission Control — система улавливания паров топлива) | Крышка бака, клапан продувки, клапан вентиляции, утечки магистралей. |
| P0597-P0599 | Thermostat heater control circuit | Электрический термостат на ряде BMW, проводка, DME. |
| P0700 | Transmission control system | Общий индикатор неисправности коробки; требуется чтение EGS (Electronic Gearbox System — электронная система коробки передач). |
| P0715/P0720 | Input/output speed sensor circuit | Датчики частоты вращения внутри коробки, мехатроник, проводка, разъемы. |
| P0730-P0736 | Incorrect gear ratio | Пробуксовка, давление масла, износ пакетов фрикционов, соленоиды, уровень/качество ATF. |
| P0741 | Torque converter clutch performance/stuck off | Блокировка гидротрансформатора, гидроблок, износ фрикциона, уровень/температура ATF. |
| U0100 | Lost communication with ECM/PCM (Engine/Powertrain Control Module — блок двигателя/силового агрегата) | Питание DME/DDE, PT-CAN, шлюз, вода в разъемах, последствия низкого напряжения. |
| U0121 | Lost communication with ABS/DSC module | Питание DSC, PT-CAN/FlexRay, датчики скорости колес, проводка. |

## 4. BMW fault memory: публичные примеры из сервисных бюллетеней

| Код | Блок | Фраза из публичного источника | Смысл для диагностики |
| --- | --- | --- | --- |
| 8013FE | IHKA | Software run time error | На ряде G-шасси публичный бюллетень связывает код с программным обеспечением IHKA и уровнем интеграции, а не с заменой блока. |
| 8013A3 | IHKA | Electric auxiliary heater: OBD temperature sensor, coolant below operating range | Пример кода IHKA на гибридных/электрических BMW; диагностика через ISTA и функциональную проверку электрического нагревателя. |
| 02010A | ACSM | Coding: coding data not qualified | Пример кодировочной ошибки при проблеме Secure Element в BCP (Basic Central Platform — базовая центральная платформа). |
| 02390A | VIP | VIP control unit: coding data not released, signature | Пример ошибки подписи/кодировочных данных. |
| 02290A | IB/DSC | DSC control unit: coding data not released, signature | Пример коммуникационно-кодировочного сбоя, где первичная причина может быть программной. |
| 020D0A | HKFM | Coding data are not qualified | Пример ошибки кодировочных данных модуля крышки багажника/задней двери. |

## 5. Индексы кодов по подсистемам

| Семейство кода | Типовая подсистема | Что проверять по данным |
| --- | --- | --- |
| P00xx-P02xx | Топливо, воздух, датчики впуска, форсунки | MAF, MAP (Manifold Absolute Pressure — абсолютное давление во впуске), коррекции топлива, давление топлива, герметичность впуска. |
| P03xx | Зажигание и пропуски воспламенения | Счетчики пропусков, катушки, свечи, форсунки, компрессия, утечки впуска. |
| P04xx | Системы выбросов | EGR, EVAP, катализатор, DPF, SCR, датчики кислорода/NOx. |
| P05xx | Скорость автомобиля и холостой ход | Датчики скорости колес, DSC, дроссель, регуляция холостого хода, утечки воздуха. |
| P06xx | Электроника блоков управления | Питание, массы, программное обеспечение, внутренняя ошибка блока, сеть. |
| P07xx-P08xx | Коробка передач | EGS, соленоиды, датчики скорости, давление ATF, уровень ATF, адаптации, мехатроник. |
| U0xxx | Сетевые связи | Шина CAN/FlexRay, шлюз ZGM, питание/массы блоков, вода, обрывы и короткие замыкания. |

## 6. Источники

- SAE_J2012_OVERVIEW: J2012: Diagnostic Trouble Code Definitions — https://saemobilus.sae.org/standards/j2012_201612-diagnostic-trouble-code-definitions
- CARB_OBD_1968_2: Title 13 California Code of Regulations § 1968.2 OBD II requirements — https://www.law.cornell.edu/regulations/california/13-CCR-1968.2
- BMW_TIS_SITE_INFO_2026: BMW Group Technical Information System Website: Site information PDF — https://bmwtechinfo.bmwgroup.com/tisUI/assets/site_information.pdf
- NHTSA_BMW_SIB_64_13_25_IHKA_8013FE_2026: SIB 64 13 25: IHKA fault code 8013FE software run time error — https://static.nhtsa.gov/odi/tsbs/2026/MC-11026960-0001.pdf
- NHTSA_BMW_SIB_12_21_16_IHKA_8013A3_2016: SI B12 21 16: Service Engine Soon (MIL) - IHKA FC 8013A3 — https://static.nhtsa.gov/odi/tsbs/2016/MC-10146814-9999.pdf
- NHTSA_BMW_SIB_61_01_26_BCP_SECURE_ELEMENT_2026: SIB 61 01 26: Secure Element failure during engine start — https://static.nhtsa.gov/odi/tsbs/2026/MC-11029070-0001.pdf
