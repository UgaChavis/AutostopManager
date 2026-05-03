# Промпт: задачи разработки для Codex

Сделай проект на Python.

## Минимальная структура

```text
app/
  models.py
  normalization.py
  scoring.py
  reporting.py
  sources/
    base.py
    forum_auto.py
    zzap.py
    manual_marketplace.py
  storage/
    sqlite_store.py
  cli.py
  tests/
    test_scoring.py
    test_normalization.py
```

## Требования

- Используй pydantic или dataclasses для моделей.
- Все внешние ключи читаются из переменных окружения.
- Не храни секреты в репозитории.
- Добавь CLI (Command-Line Interface — интерфейс командной строки):
  - `parts search --request sample.json`
  - `parts score --offers offers.jsonl`
  - `parts report --case case.json`
- Добавь unit tests (модульные тесты).
- Добавь README по запуску.

## Offline MVP

Сначала реализуй работу без внешних API на файлах:

- `data/sample_part_requests.jsonl`
- `data/sample_offers_synthetic.jsonl`

## Затем

Подготовь интерфейсы коннекторов, но реальные вызовы подключай только после выдачи ключей и проверки условий источника.
