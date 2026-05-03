# Промпт: постановка задачи API-поиска

Задача: подготовить план запросов к разрешенным источникам.

## Вход

```json
{fitment_result_json}
```

## Выход

```json
{
  "queries": [
    {
      "source_id": "forum_auto",
      "query_type": "article_search",
      "brand": "",
      "article": "",
      "include_crosses": true,
      "priority": 1,
      "reason": ""
    }
  ],
  "manual_queries": [],
  "do_not_query": []
}
```

## Правила

- Запрашивай точный OEM первым.
- Потом замененные OEM-номера.
- Потом надежные кроссы.
- Для Авито/Дром/FarPost формируй только ручные поисковые фразы или используй официально разрешенный API.
- Не запускай запрос, если применяемость unsafe.
