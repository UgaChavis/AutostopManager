# Офлайн-каталоги запчастей

Сценарий: [E3](../modules/E3.md). Source: [offline_catalogs.py](../../../autostop_manager/offline_catalogs.py). Private index — `data/offline_parts_catalogs/catalog_index.json`, производный текст — соседний text/. Root по умолчанию рядом с `AUTOSTOP_MANAGER_DB`; отдельный разрешённый release может задать абсолютный `AUTOSTOP_OFFLINE_CATALOG_ROOT` на общий cache. Индексация исходников не меняет runtime config.

## Поиск

`search_offline_parts_catalogs`: query (article/OE/обезличенный vehicle profile), необязательные catalog_id/brand, limit 1..20. Full VIN отклоняется. Поиск read-only/local, требует `/usr/bin/rg`, ограничивает время/размер/число файлов/результатов; интернет не вызывается.

Ответ: bounded excerpt, publisher/title/edition/market/limitations, URL без query и PDF page либо XLSX sheet/row. `search_incomplete`/`skipped_catalog_ids` — выбранное множество просмотрено не полностью; `partially_extracted_catalog_ids` — incomplete text extraction. Завершённый OCR сам не делает поиск неполным. OCR match содержит `extraction_method=ocr`, `ocr_unverified=true` и требует сверки с исходной страницей. Synthetic `[PAGE N]` новых PDF исключается до match count; старый PDF сохраняет точный текстовый поиск.

Находка — кандидат: сверяй vehicle modification, engine, market, размеры и VIN/OEM fitment. XLSX числовые форматы могут отличаться от отображаемого артикула; формулы не исполняются, внешние ссылки не читаются. Исходные PDF/XLSX не помещаются в Git.

## Pinned release sync

Архив 25 каталогов: [GitHub catalogs-2026-09-25](https://github.com/UgaChavis/AutostopManager/releases/tag/catalogs-2026-09-25), `autostop-parts-catalogs-20260925-public.zip`, SHA-256 `b2b1e40510e91579a5e8e4be90e0d4a2b461122b6a73af27ec4600d4a1ff7ffe` (отдельный sha256 asset).

Согласованный `/opt/autostopcrm/deploy.sh` до maintenance вызывает sync из выбранного Manager snapshot: сверка 25 originals/SHA/index/text и synthetic search → при ready без download → при пустом cache pinned ZIP с size/SHA check, private import и OCR → при незавершённом OCR только его продолжение. Старые каталоги сохраняются. Download/hash/import/OCR/search failure останавливает выпуск до maintenance. Если индексированная запись есть, но original/text утрачен или повреждён, sync останавливается без перезаписи: importer автоматически её не восстанавливает.

Только чтение без download/записи:

```bash
PYTHONSAFEPATH=1 PYTHONPATH=/opt/AutostopManager python3 /opt/AutostopManager/scripts/sync_offline_parts_catalog_release.py --cache-root /opt/AutostopManager/data/offline_parts_catalogs --verify-only
```

Для отдельно порученной установки используй ту же команду без --verify-only. Скрипт не читает DB/.env, принудительно использует cache-root. Git checkout не содержит каталогов и сам их не импортирует.

## Импорт и OCR

Нужны Python 3.11+, Poppler (`pdfinfo`, `pdftotext`); для пустых страниц ещё `pdftoppm`, Tesseract eng/rus. Verification read-only:

```bash
python scripts/import_offline_parts_catalogs.py --archive /path/to/autostop-parts-catalogs-20260925-public.zip --cache-root /opt/AutostopManager/data/offline_parts_catalogs --verify-only
python scripts/ocr_offline_parts_catalogs.py --cache-root /opt/AutostopManager/data/offline_parts_catalogs --verify-only
```

При порученном изменении убери --verify-only. Importer проверяет пути/CRC/SHA, сохраняет originals и производные файлы в private data, пропускает уже добавленные записи, меняет index последним. PDF извлекается по страницам, empty_pages требует OCR; XLSX потоковый без formulas/external links. OCR меняет только пустые страницы TXT, ставит `[OCR UNVERIFIED: eng+rus]`, обновляет index последним, originals не меняет. Повторный запуск пропускает учтённое и завершает прерванный index update.
