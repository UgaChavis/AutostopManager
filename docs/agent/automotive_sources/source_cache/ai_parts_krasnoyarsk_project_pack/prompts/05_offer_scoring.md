# Промпт: оценка предложений

Задача: оценить список предложений и выбрать быстрый, дешевый и сбалансированный варианты.

## Вход

```json
{
  "request": {part_request_json},
  "offers": [{offer_json}],
  "mode": "standard|urgent_today|used_or_contract|electronics"
}
```

## Выход

```json
{
  "ranked_offers": [],
  "best_fast": null,
  "best_price": null,
  "best_balanced": null,
  "market_stats": {
    "median_price_rub": null,
    "min_price_rub": null,
    "max_price_rub": null,
    "outliers": []
  },
  "manager_summary": "",
  "must_verify_before_order": []
}
```

## Правила

- Не сравнивай новую и б/у деталь в одной ценовой медиане.
- Не сравнивай OEM и сомнительный аналог без флага риска.
- Предложение без срока не может быть лучшим для срочного ремонта.
- Предложение без подтвержденного номера не может быть лучшим для электронного блока.
- Подозрительно низкая цена должна снижать итоговый рейтинг.
