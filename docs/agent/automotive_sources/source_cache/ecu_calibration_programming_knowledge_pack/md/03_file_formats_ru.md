# Форматы файлов прошивок, калибровок и измерений

Файл для ECU может быть исполняемым кодом, контейнером с сегментами, калибровочным набором, диагностическим описанием, логом измерений или конфигурацией. Нельзя оценивать файл только по расширению. Один и тот же тип данных может быть упакован в разные форматы в зависимости от производителя, ECU, рынка и инструмента.

## Главное разделение

- Firmware image: исполняемый код и данные для записи в ECU.
- Calibration image: калибровочные данные, иногда отдельный сегмент, иногда часть общего образа.
- Description file: описание переменных, адресов, единиц, преобразований и диагностических сервисов.
- Container: упаковка нескольких сегментов, подписей, метаданных и условий совместимости.
- Measurement log: запись сигналов для анализа.
- Coding/configuration: параметры комплектации и вариантного кодирования.

## BIN

BIN - сырой бинарный образ. Он может содержать весь flash, один сегмент или только calibration area. Внутри нет обязательной структуры имени, адресов или метаданных. Без описания риск неверной интерпретации максимален.

## Intel HEX

Intel HEX - текстовый формат, где каждая строка содержит адрес, тип записи, данные и контрольную сумму. Используется в embedded-разработке и учебных примерах.

```text
:020000040001F9
:100000000102030405060708090A0B0C0D0E0F1068
:00000001FF
```

Строка начинается с двоеточия. Поля: длина, адрес, тип записи, данные, checksum. Это не означает, что любой ECU принимает Intel HEX напрямую.

## Motorola S-record

Motorola S-record - текстовый формат с адресами, данными и checksum.

```text
S0030000FC
S11300000102030405060708090A0B0C0D0E0F1063
S9030000FC
```

Он удобен для контроллеров и программаторов, но в автомобильном сервисе чаще встречаются OEM-контейнеры.

## ELF и MAP

ELF (Executable and Linkable Format - исполняемый и компонуемый формат) и MAP (linker map - карта компоновщика) характерны для разработки. Они могут содержать символы, секции, адреса и служебную информацию. В сервисе их обычно нет, потому что серийные ECU поставляются как закрытые и подписанные пакеты.

## A2L / ASAM MCD-2 MC

A2L описывает измеряемые переменные и калибровочные параметры ECU. Он сообщает инструменту, где находится значение, как его масштабировать, какие оси у карты и какие единицы измерения использовать.

Типовые элементы A2L:

- PROJECT: проект.
- MODULE: описание одного ECU или software module.
- MEASUREMENT: измеряемая переменная.
- CHARACTERISTIC: калибровочный параметр, кривая или карта.
- AXIS_DESCR: описание оси.
- COMPU_METHOD: метод преобразования raw-to-physical.
- RECORD_LAYOUT: способ хранения в памяти.

Учебный фрагмент:

```text
/begin CHARACTERISTIC ENG_TEMP_LIMIT
  "Synthetic training scalar"
  VALUE 0x00001234 UWORD_CM 0 200 degC
  COMPU_METHOD TEMP_SCALE
/end CHARACTERISTIC
```

A2L сам по себе не является прошивкой. Он описывает, как читать или изменять данные при наличии правильного доступа, правильного образа и разрешенного процесса.

## DCM

DCM (Data Calibration Management) - текстовый формат для хранения калибровочных значений. Он часто связан с workflow инженерной калибровки. Для сервиса важен как пример того, что значение калибровки и описание калибровки - разные вещи.

```text
KONSERVIERUNG_FORMAT 2.0
FESTWERT
  NAME ENG_TEMP_LIMIT
  WERT 108.0
END
```

## CDF / CDFX

CDF (Calibration Data Format) - XML-ориентированный формат ASAM для хранения значений параметров и метаданных калибровочного процесса. В отличие от A2L, CDF хранит значения, а A2L описывает, что эти значения означают.

## ODX / PDX

ODX (Open Diagnostic Data Exchange) описывает диагностические возможности ECU: идентификаторы, сервисы, DTC, параметры, сеансы, кодировки. PDX (Packaged ODX) - пакет ODX-данных. В сервисе ODX лежит за диагностическими приложениями: пользователь видит тест-план, а инструмент использует диагностическое описание.

Учебный фрагмент ODX:

```xml
<DIAG-SERVICE ID="ReadVIN">
  <SHORT-NAME>ReadVIN</SHORT-NAME>
  <SEMANTIC>READ-DATA-BY-IDENTIFIER</SEMANTIC>
</DIAG-SERVICE>
```

## MDF / MF4

MDF (Measurement Data Format) - бинарный формат для измерительных данных. Он хранит сигналы, временные метки, каналы, единицы, метаданные. В ремонте полезен для логов: например, обороты, давление наддува, commanded EGR, actual EGR, DPF differential pressure, NOx upstream/downstream.

## BLF, ASC и DBC

BLF (Binary Logging Format - бинарный формат логов) и ASC (ASCII log - текстовый лог) часто используются для записи CAN-сообщений. DBC (Database CAN - база описания CAN-сообщений) описывает CAN-сообщения, сигналы, битовые позиции, масштаб и единицы. Неверный DBC дает неверные выводы.

## Контроль целостности и подпись

Checksum нужна для обнаружения повреждений. В ECU она может быть простой суммой, CRC, несколькими областями, таблицей блоков или частью криптографической проверки. Современные ECU используют подписи, зашифрованные контейнеры, secure boot, anti-rollback и hardware security module. Сервисная процедура должна использовать авторизованный инструмент, который сам проверяет совместимость и подлинность пакета.

## На каких языках пишутся парсеры

- Python: быстрые инструменты для анализа логов, CSV, JSON, XML, HEX.
- C/C++: низкоуровневые встроенные системы, firmware, быстрые библиотеки.
- Rust: безопасные парсеры бинарных форматов.
- Java/C#: диагностические приложения, GUI (Graphical User Interface - графический интерфейс пользователя), backend.
- MATLAB/Simulink: моделирование и автогенерация кода в инженерной разработке.
- XML/JSON/YAML: не языки программирования, а форматы данных для описаний и конфигураций.

## Минимальная логика чтения Intel HEX

```text
read line
verify ':'
read byte_count, address, record_type, data, checksum
verify checksum
if record_type == data:
    place bytes at effective_address
if record_type == extended_linear_address:
    update address high word
if record_type == end_of_file:
    stop
```

Это учебная схема чтения формата. Она не дает пригодного процесса записи ECU и не заменяет OEM-инструмент.

## Признаки несовместимого файла

- Не совпадает hardware number.
- Не совпадает software compatibility ID.
- Пакет предназначен для другого рынка или нормы выбросов.
- Не совпадает трансмиссия или привод.
- VIN-зависимые данные отсутствуют или конфликтуют.
- Tool сообщает invalid signature, wrong session, request out of range, programming abort.
- После записи блок требует кодирование, которого нет в плане.

## Практический вывод

Файл ECU надо рассматривать не как набор байтов, а как часть цепочки: описание - совместимость - подпись - диагностический протокол - питание - запись - проверка - кодирование - адаптация - валидация.


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
