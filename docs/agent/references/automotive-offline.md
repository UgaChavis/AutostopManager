# Локальные автомобильные декодеры

Подготовка библиотек и базы — отдельная техническая стадия. Обычные
`decode_wmi_local`, `decode_frame_local`, `vin_brand_details`, `vininfo_decode`
и `corgi_decode` не устанавливают пакеты, не скачивают данные и не переключают
декодер. Вызовы доступны через native Manager MCP с этими именами;
stable tool ID — `manager.<имя>`. Точные входы — live inputSchema.

## Реализация и покрытие

| Инструмент | Источник и область | Граница результата |
|---|---|---|
| `decode_wmi_local(wmi)` | Общий `automotive_local_registry.py`, версия v1 | Ровно 3 символа WMI; assembler/country hints, без модели/двигателя/рынка VIN |
| `decode_frame_local(identifier)` | Прежние правила Suzuki MR41S и Honda ES1 | Семейные кандидаты; другие frame дают unsupported; VIN не подменяется frame |
| `vin_brand_details(identifier, context=None)` | Проверенные прежние Audi/Volkswagen/Skoda prefix rules | Только family/platform hints; engine/KPP/options/date остаются неизвестными, конфликт context сохраняется |
| `vininfo_decode(identifier)` | vininfo 1.11.0, локальные таблицы | Manufacturer/region/country и доступные brand details; несколько модельных лет остаются вариантами |
| `corgi_decode(identifier, model_year=None, timeout_seconds=10)` | Corgi 2.0.4 и его подготовленная SQLite | Доступные VIN-поля; Северная Америка — основное vPIC покрытие, другие рынки частичны; override model_year явный |

WMI_HINTS/PLATFORM_RULES/PlatformRule перенесены в один реестр без изменения
прежних правил. Составной `vehicle_identity` импортирует именно этот реестр.
Дублированные WMI/platform сведения не становятся независимыми свидетельствами.
Новые входы не повышают старую эвристику до заводской комплектации.

Результат использует `autostop.automotive-result.v1`: tool_id, outcome, data,
evidence, missing_fields, conflicts, warnings, execution.network_calls=0.
VIN/frame binding — kind и SHA256, без исходного частного идентификатора.
Результат декодирования хранит model_year отдельно от production_date.
Invalid input, unsupported coverage, dependency_missing, database_missing,
configuration_missing и parse_error различаются. Timeout сохраняется как
parse_error с warning=decoder_timeout; повторного decoder/network fallback нет.
Checksum mismatch — отдельный исход проверки; он не является универсальным
запретом для рынков без обязательной SAE контрольной цифры.

## Зависимости и лицензии

Версии, SHA256 архивов и базы зафиксированы в
[automotive_offline_sources.json](../automotive_offline_sources.json).
[vininfo](https://github.com/idlesign/vininfo) — BSD-3-Clause,
[PyPI 1.11.0](https://pypi.org/project/vininfo/1.11.0/) требует Python>=3.10;
библиотечный вызов не требует click/CLI extra.
[Corgi](https://github.com/cardog-ai/corgi) — ISC,
[npm 2.0.4](https://www.npmjs.com/package/@cardog/corgi/v/2.0.4).
Обе полные upstream лицензии сохраняются внутри prepared runtime.

Corgi пакет содержит customized [NHTSA vPIC](https://vpic.nhtsa.dot.gov/api/)
SQLite и community patterns. Это та же первичная vPIC lineage, что у online
vPIC; две обёртки не считаются двумя независимыми подтверждениями.
Дата исходного NHTSA среза у bundled базы не объявляется известной:
database_version=`bundled-with-corgi-2.0.4`, точная SHA в manifest.
У подготовленного среза 12 297 WMI и 878 324 pattern rows; это размеры
среза, а не гарантия поддержанного VIN/рынка/комплектации.

Используется официальный `dist/browser.mjs` export `decodeVIN` с небольшим
local read-only adapter для [Node SQLite](https://nodejs.org/docs/latest-v24.x/api/sqlite.html).
`createDecoder/getDatabasePath/quickDecode` не вызываются. Поэтому npm CLI,
better-sqlite3, commander, sql.js, скачивание кэша и native addon сборка
не требуются. Нужен Node>=22.13.0; конкретные version/executable/SHA аттестуются
при подготовке и проверяются при decode. Telegram Python не изменяется.

## Воспроизводимая подготовка

Подготовленный runtime принадлежит инструментам Manager и отделён от его
Python зависимостей. Канонический default path:
`/var/lib/autostop-automotive-offline/current`. Явный environment selector:
`AUTOSTOP_AUTOMOTIVE_OFFLINE_RUNTIME`. Подготовка не меняет current/services.

```bash
python3 scripts/prepare-automotive-offline.py --output /NEW_OWNED_PARENT/runtime
python3 scripts/check-automotive-offline.py --runtime /NEW_OWNED_PARENT/runtime
```

Output обязан быть новым каталогом под существующим owned parent.
При наличии архивов передай `--corgi-archive EXACT_TGZ --vininfo-wheel EXACT_WHL`:
их bytes/SHA проверяются тем же lock, установка из сети не нужна.
`--node EXACT_EXECUTABLE` выбирает аттестуемый Node. Произвольные версии,
непроверенные upstream latest и подготовка внутри Telegram venv исключены.

Corgi tgz=26 366 743 байта, bundled database=80 355 328 байт.
Preflight требует 1 GiB резерва плюс два archive и два expanded DB размера;
архив ограничен 40 MB, разжатая база 160 MB. Это отдельный bounded budget,
который не заменяет deploy/full-backup reserves.
Сначала проверяются pinned archive hashes, затем выбранные regular members,
без tar extractall/symlinks; wheel извлекается с проверкой относительных путей.
Database проходит полную integrity_check. Manifest сохраняет version,
licenses, file hashes, Node hash, record counts и время подготовки.
Незавершённый новый каталог не считается runtime и не активируется.

Перед согласованным выпуском координатор готовит/проверяет immutable runtime
из опубликованной инструкции и фиксирует его в release tuple. Активация
default current/selector, readback и сохранение previous runtime выполняются
с остальным релизом; одна подготовка не является installed acceptance.
Не менять active Node без нового offline-runtime gate: pin mismatch закрывает decode.

## Offline acceptance и исполнение

`check-automotive-offline.py` читает только опубликованные примеры библиотек
и синтетические локальные ID. Он проверяет реальные subprocesses,
положительный результат, invalid-input и unsupported вне локального покрытия;
receipt содержит технические flags, не private vehicle fields.
На Linux дополнительное испытание без сети:

```bash
unshare -n -- python3 scripts/check-automotive-offline.py --runtime /EXACT_RUNTIME
```

Runtime workers получают VIN по stdin, не argv. Python запускается `-I -B`
и импортирует лишь attested vininfo directory. Corgi запускается без shell,
с Node permission model, без child-process/addon/write grants и с ограниченными
read paths. Workers закрывают публичные socket/HTTP/fetch/DNS API; сам выбранный
decode core обращается только к local readonly SQLite. No-network stage
дополнительно подтверждается isolated network namespace.
На installed MCP с RestrictNamespaces создание новой namespace может быть
запрещено: ordinary decode использует тот же pinned no-network code path,
не создаёт namespace и не требует снятия systemd guards.

Каждый decode независимо проверяет hashes только своего provider; отсутствие
Corgi DB не блокирует vininfo. Timeout<=30s, output<=100KB, credentials/NODE_OPTIONS
не наследуются, errors не возвращают raw stderr/customer identifier.
Нет factory EPC, OE/fitment доказательства или полномочия CRM/Store записи.
Полное сохранение prepared/current/previous runtime необходимо для rollback;
database/licences нельзя удалять как generic cache.
