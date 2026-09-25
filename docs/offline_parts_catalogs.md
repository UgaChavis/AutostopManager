# Офлайн-каталоги запчастей

Локальный индекс находится в `data/offline_parts_catalogs/catalog_index.json`; извлечённый текст — в `text/` рядом с ним. Существующие каталоги и новые файлы из предоставленного архива используются как справочные данные по артикулам, заменам и применимости. Совпадение является кандидатом: перед подбором подтвердите модификацию, двигатель, рынок, размеры и применимость по VIN/OEM-источнику.

MCP-инструмент `search_offline_parts_catalogs` принимает `query` (артикул, OE-ссылку или обезличенный профиль автомобиля), необязательные `catalog_id` и `brand`, а также `limit` от 1 до 20. Полный VIN в запросе отклоняется. Результат содержит короткий фрагмент, издателя, название, редакцию, рынок, ограничения каталога, ссылку на источник без параметров URL и координату: страницу PDF либо лист и строку XLSX. `search_incomplete=true` и `skipped_catalog_ids` показывают, что ограничение времени, размера, числа файлов или результата помешало просмотреть всё выбранное множество. Неполное извлечение текста отмечается в `partially_extracted_catalog_ids`; завершённый OCR не делает поиск неполным. Для PDF-совпадений `extraction_method="ocr"` и `ocr_unverified=true` требуют сверки артикула с исходной страницей.

Поиск читает только локальные файлы, не изменяет каталог и не обращается к интернету. Он использует `rg` и ограничивает запрос, число результатов, объём фрагмента, время и размер сканируемых файлов. Синтетические строки `[PAGE N]` в новых PDF исключены до подсчёта совпадений; старые PDF сохраняют точный поиск по тексту. Расположение каталога по умолчанию: `offline_parts_catalogs` рядом с БД, заданной `AUTOSTOP_MANAGER_DB`; для отдельного runtime-release можно установить `AUTOSTOP_OFFLINE_CATALOG_ROOT` на абсолютный путь к общему кэшу. Изменение переменной требует отдельного разрешённого релиза и не выполняется индексацией исходников.

Для выполнения поиска нужен установленный ripgrep (`rg`).

## Импорт

Архив 25 новых каталогов опубликован в [GitHub Release catalogs-2026-09-25](https://github.com/UgaChavis/AutostopManager/releases/tag/catalogs-2026-09-25). SHA-256 скачанного `autostop-parts-catalogs-20260925-public.zip`: `b2b1e40510e91579a5e8e4be90e0d4a2b461122b6a73af27ec4600d4a1ff7ffe`. В выпуске есть отдельный файл `.sha256`.

При следующем согласованном обновлении сервера `/opt/autostopcrm/deploy.sh` до начала обслуживания вызывает скрипт из выбранного снимка Manager. Скрипт проверяет 25 исходных файлов по SHA-256, их записи в индексе, извлечённый текст и поисковый запрос через `search_offline_parts_catalogs`. Если всё готово, ZIP не скачивается. На пустом кэше скрипт скачивает закреплённый ZIP из GitHub Release, проверяет размер и SHA-256, создаёт закрытый индекс, импортирует PDF/XLSX и выполняет OCR пустых страниц. При наличии только незавершённого OCR он заканчивает OCR без повторной загрузки. Кэш расположен рядом с БД Manager, вне Git; старые каталоги и их записи сохраняются. Ошибка загрузки, контрольной суммы, импорта, OCR или поиска прерывает обновление до обслуживания. Повторный запуск безопасен. Если запись уже есть, но её оригинал или поисковый файл удалён либо повреждён, скрипт останавливается без перезаписи этой записи: существующий импортёр не восстанавливает индексированные файлы автоматически.

Для чтения состояния без загрузки и записи:

```bash
PYTHONSAFEPATH=1 PYTHONPATH=/opt/AutostopManager python3 /opt/AutostopManager/scripts/sync_offline_parts_catalog_release.py --cache-root /opt/AutostopManager/data/offline_parts_catalogs --verify-only
```

Для отдельной установки на сервере с уже существующим каталогом `data/`:

```bash
PYTHONSAFEPATH=1 PYTHONPATH=/opt/AutostopManager python3 /opt/AutostopManager/scripts/sync_offline_parts_catalog_release.py --cache-root /opt/AutostopManager/data/offline_parts_catalogs
```

Для поиска нужен исполняемый `/usr/bin/rg`, доступный системной службе MCP. При новом импорте нужны `pdfinfo` и `pdftotext`; только для страниц, требующих OCR, нужны `pdftoppm`, Tesseract и языки `eng` и `rus`. Скрипт не открывает БД или `.env`: поисковая проверка принудительно использует указанный `--cache-root`. Git checkout сам по себе не содержит PDF/XLSX и не выполняет импорт; файлы попадают в кэш при согласованном запуске обновления через `/opt/autostopcrm/deploy.sh`.

Для проверки архива и плана без записи файлов:

```bash
python scripts/import_offline_parts_catalogs.py --archive /path/to/autostop-parts-catalogs-20260925-public.zip --cache-root /opt/AutostopManager/data/offline_parts_catalogs --verify-only
```

Для индексации в существующий закрытый кэш:

```bash
python scripts/import_offline_parts_catalogs.py --archive /path/to/autostop-parts-catalogs-20260925-public.zip --cache-root /opt/AutostopManager/data/offline_parts_catalogs
```

Нужны Python 3.11+, `pdfinfo` и `pdftotext` из Poppler. Импортёр проверяет пути, CRC и SHA-256, сохраняет исходные PDF/XLSX и извлечённые данные в `data/` вне Git, оставляет существующие записи и повторно пропускает уже добавленные каталоги. Индекс заменяется после записи новых файлов. PDF разбиваются по страницам; страницы без извлечённого текста перечисляются в `empty_pages` и требуют отдельного OCR. XLSX читаются потоково без выполнения формул и обращения к внешним ссылкам; числовые значения с нестандартным форматом Excel могут отличаться от отображаемого артикула, поэтому сверяйте их с оригиналом.

Для страниц из `empty_pages` доступен отдельный OCR-проход (`pdftoppm`, Tesseract с `eng+rus`):

```bash
python scripts/ocr_offline_parts_catalogs.py --cache-root /opt/AutostopManager/data/offline_parts_catalogs --verify-only
python scripts/ocr_offline_parts_catalogs.py --cache-root /opt/AutostopManager/data/offline_parts_catalogs
```

OCR изменяет только пустые страницы в производных TXT, отмечает их `[OCR UNVERIFIED: eng+rus]` и обновляет индекс последним. Исходные PDF остаются без изменений. Результат распознавания требует сверки с изображением страницы; повторный запуск пропускает уже учтённые страницы и умеет завершить прерванное обновление индекса.
