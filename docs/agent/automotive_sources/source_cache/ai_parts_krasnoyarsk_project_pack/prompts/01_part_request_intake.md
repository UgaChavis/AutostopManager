# Промпт: прием заявки на запчасть

Задача: превратить свободный текст менеджера в структурированную заявку.

## Вход

Свободный текст:

```text
{user_message}
```

## Выход

Верни JSON (JavaScript Object Notation — объектная нотация JavaScript):

```json
{
  "vehicle": {
    "make": null,
    "model": null,
    "year": null,
    "vin_or_frame": null,
    "body_code": null,
    "engine_code": null,
    "transmission_code": null,
    "drive": null
  },
  "part": {
    "name_raw": null,
    "name_normalized": null,
    "oem_number": null,
    "article_candidates": [],
    "side": null,
    "position": null,
    "condition_allowed": ["new"]
  },
  "constraints": {
    "city": "Красноярск",
    "urgency": "unknown",
    "max_budget_rub": null,
    "delivery_allowed": true
  },
  "missing_fields": [],
  "risk_flags": []
}
```

## Правила

- Если VIN отсутствует, добавь `missing_fields: ["vin_or_frame"]` для VIN-зависимых деталей.
- Если деталь имеет сторону, но сторона не указана, добавь `side` в missing_fields.
- Если указан бытовой термин, нормализуй его: «граната» → «ШРУС (шарнир равных угловых скоростей)».
- Не придумывай OEM-номер.
- Если данные противоречивы, добавь risk_flags.
