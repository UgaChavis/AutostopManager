# Сети автомобиля, OBD II и UDS

Диагностика и программирование зависят от связи между тестером и ECU. Ошибка связи может выглядеть как неисправный блок, но часто первопричина - питание, масса, wake-up, шлюз, несовместимый VCI, перегруженная сеть или неверный протокол.

## Основные автомобильные сети

- CAN (Controller Area Network - сеть контроллеров): основная сеть силового агрегата, шасси, кузова. Классический CAN имеет полезную нагрузку до 8 байт, CAN FD (Flexible Data-rate - гибкая скорость передачи данных) - больше.
- LIN (Local Interconnect Network - локальная межсоединительная сеть): простая сеть для датчиков, приводов, кнопок, зеркал, климатических заслонок.
- FlexRay: высокоскоростная детерминированная сеть, встречается в шасси и некоторых старших платформах.
- Ethernet: используется для DoIP, камер, ADAS (Advanced Driver Assistance Systems - системы помощи водителю), обновлений и высоких объемов данных.
- K-Line: старый однопроводный интерфейс диагностики.
- MOST (Media Oriented Systems Transport - мультимедийная оптическая шина): инфотейнмент в старых премиальных автомобилях.

## OBD II и OEM-диагностика

OBD II - обязательный диагностический слой для экологически значимых систем. Он дает generic PID (Parameter ID - идентификатор параметра), readiness, freeze frame и generic DTC. OEM-диагностика глубже: она содержит специфические коды, процедуры, тест-планы, адаптации, калибровочные статусы и программирование.

OBD II отвечает на вопрос: есть ли экологически значимая неисправность. OEM-диагностика отвечает на вопрос: какой компонент, цепь, режим или тест-план связан с неисправностью именно в этой модели.

## ISO-TP поверх CAN

Один CAN-кадр мал для длинных диагностических сообщений. ISO-TP (ISO Transport Protocol) делит длинное сообщение на кадры:

- Single Frame: короткое сообщение помещается в один кадр.
- First Frame: начало длинного сообщения, указывает общую длину.
- Flow Control: получатель сообщает, сколько кадров и с какой паузой можно отправлять.
- Consecutive Frame: последующие части сообщения.

Для программирования это критично: файл передается блоками, а ECU подтверждает прием. Потеря кадра или нарушение тайминга может прервать запись.

## UDS: общая логика

UDS (Unified Diagnostic Services - унифицированные диагностические сервисы) - набор диагностических сервисов между tester client и ECU server. В ремонте встречаются следующие категории:

- Сессии: обычная, расширенная, программирования.
- Идентификация: чтение VIN, software number, hardware number, calibration ID.
- DTC: чтение, очистка, freeze frame, статусы.
- Data by identifier: live data и служебные параметры.
- Routines: процедуры тестирования, адаптации, проверки.
- IO control: временное управление исполнительными механизмами.
- Download/upload: запись или передача данных в рамках разрешенного процесса.
- ECU reset: перезапуск после процедуры.

## Типовые UDS-сервисы, которые надо знать концептуально

```text
0x10 DiagnosticSessionControl - смена диагностической сессии
0x11 ECUReset - перезапуск ECU
0x14 ClearDiagnosticInformation - очистка DTC
0x19 ReadDTCInformation - чтение DTC
0x22 ReadDataByIdentifier - чтение данных по DID
0x27 SecurityAccess - авторизованный доступ, только официально
0x28 CommunicationControl - управление коммуникацией
0x2E WriteDataByIdentifier - запись разрешенного DID
0x31 RoutineControl - запуск процедуры
0x34 RequestDownload - запрос загрузки данных в ECU
0x36 TransferData - передача блока данных
0x37 RequestTransferExit - завершение передачи
```

Знание сервисов нужно для понимания логов и ошибок. Оно не заменяет официальный flash tool и не дает права обходить защиту доступа.

## Negative Response Codes

NRC (Negative Response Code - код отрицательного ответа) объясняет, почему ECU отказал:

- 0x10 GeneralReject: общий отказ.
- 0x11 ServiceNotSupported: сервис не поддерживается.
- 0x12 SubFunctionNotSupported: подфункция не поддерживается.
- 0x13 IncorrectMessageLengthOrInvalidFormat: неверная длина или формат.
- 0x22 ConditionsNotCorrect: условия не выполнены.
- 0x31 RequestOutOfRange: параметр вне диапазона.
- 0x33 SecurityAccessDenied: нет разрешенного доступа.
- 0x35 InvalidKey: неверный ключ.
- 0x36 ExceedNumberOfAttempts: превышены попытки.
- 0x37 RequiredTimeDelayNotExpired: задержка не истекла.
- 0x78 ResponsePending: ECU еще выполняет операцию.

В программировании важно отличать отказ из-за условий от отказа из-за поврежденного блока. Например, ConditionsNotCorrect может означать неправильное напряжение, активную ошибку сети, не тот режим зажигания, блокировку иммобилайзера или несовместимость.

## DoIP

DoIP (Diagnostics over Internet Protocol - диагностика по IP) используется для больших объемов данных и современных платформ. Тестер подключается через Ethernet, выполняет обнаружение автомобиля, устанавливает TCP-соединение и передает UDS-полезную нагрузку. Для сервиса это быстрее при программировании, но требует стабильной сети, совместимого интерфейса и корректной маршрутизации через gateway.

## J2534 pass-thru

J2534 - стандарт SAE (Society of Automotive Engineers - Общество автомобильных инженеров) для совместимости ПО производителя автомобиля с pass-thru интерфейсом. Идея: независимая мастерская использует официальное ПО OEM и совместимый интерфейс для диагностики и программирования, когда производитель это поддерживает. J2534 не означает универсальную возможность прошивать любые блоки любыми файлами.

## Структура DTC

Generic OBD II код имеет вид P0xxx, B0xxx, C0xxx, U0xxx:

- P - Powertrain - двигатель и трансмиссия.
- B - Body - кузовная электроника.
- C - Chassis - шасси.
- U - Network - сеть и коммуникация.
- 0 - generic, определен стандартом.
- 1, 2, 3 - manufacturer-specific или расширенные варианты.

Статус DTC важнее самого кода: current/active, pending, stored, history, confirmed, permanent. Freeze frame фиксирует условия появления DTC, а live data показывает текущие значения. Для ремонта важна связка: DTC, статус, freeze frame, live data, тест исполнительного механизма, электропроверка и механическая проверка.

## Диагностический порядок при сетевой проблеме

1. Проверить АКБ, массу, питание ECU и предохранители.
2. Проверить наличие wake-up и клеммы 15.
3. Проверить CAN High/CAN Low сопротивление и короткое замыкание.
4. Проверить, видит ли gateway нужный ECU.
5. Проверить ошибки U-класса в соседних блоках.
6. Проверить версию ПО VCI и диагностического приложения.
7. Только после этого подозревать повреждение ECU.


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
