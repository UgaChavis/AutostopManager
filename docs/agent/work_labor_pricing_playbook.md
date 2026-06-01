# Work Labor Pricing Playbook

Purpose: estimate AutoStop labor price for repair work from public Russia STO
labor-only prices and add a second public labor-time / norm-hours plausibility
layer. The labor-time layer does not replace the price basis. This route is
read-only: it prepares a manager estimate but does not write repair-order
works, materials, prices, payments, or cashbox data.

## Trigger

Use this playbook when the owner asks:

- сколько стоит работа
- оценить стоимость работ
- смета по работам
- работа без запчастей
- средняя цена по России плюс 50%
- нормо-часы по работе
- трудоемкость ремонта

Use `estimate_repair_work_cost` through MCP or:

```powershell
python -m autostop_manager.cli estimate-work --vehicle "BMW X5" --work "замена рулевой рейки"
```

## Workflow

1. Identify the vehicle: VIN/chassis when available, then make, model, year,
   engine, transmission, drivetrain, and city.
2. Normalize the requested work into exact labor operations. Example:
   `поменять рейку`, `замена рулевой рейки`, and
   `рулевая рейка снять/поставить` are one operation:
   `замена рулевой рейки`.
3. If the input is only a complaint, do not price the final repair. Price only
   diagnostics when public prices exist, then return a diagnostic checklist and
   missing context for the final estimate.
4. Collect public Russia STO prices for the same operation and comparable
   vehicle class. Keep only labor-only prices. Do not mix work prices with
   parts, fluids, programming licenses, towing, or aggregates.
5. Separately search public labor-time language: `нормо-часы`, `норма
   времени`, `трудоемкость`, `время выполнения`, and `снять/установить`. Use
   only public web rows/snippets and never call closed RMI/EPC accounts,
   CAPTCHA-gated pages, paid databases, or owner-provided manual norm-hours.
6. Store a short market sample only: source, city/region, operation name,
   price, `includes_parts=false`, capture date, and quote confidence. Do not
   store full HTML pages, full price lists, or raw screenshots in memory/CRM.
7. Store a short labor-time sample only: source, operation, hours/range, capture
   date, confidence, and whether the source is public. Do not call it official
   OEM norm-hours unless the public source itself is an official open source.
8. Exclude obvious outliers. Then calculate:
   `russia_average_rub = arithmetic_mean(valid_public_quotes_after_outlier_filter)`.
9. Calculate AutoStop labor price:
   `autostop_price_rub = round_to_100(russia_average_rub * 1.50)`.
10. Use labor-time only as the second layer: plausibility check, effective
    hourly-rate check, and overlap detection. Norm-hours alone must not create a
    confident AutoStop price.
    Missing public labor-time data should set `labor_time_confidence=blocked`
    and add a next action. It does not by itself override a valid market
    estimate from 3+ comparable labor-only quotes; lower the overall confidence
    when the job is complex, safety-critical, or still too broad.
11. If fewer than 3 valid comparable labor-only quotes remain, return
   `confidence=low` and do not present the AutoStop price as confident.
12. Before a repair-order write, require a separate explicit owner command and a
   live CRM target. This tool must not call `replace_repair_order_works`.

## Source Basis

Primary basis: public STO price lists and public service pages across Russia.

Use directories/search as discovery routes:

- official STO public pages
- city/subdomain price pages of national or regional networks
- 2GIS and Yandex Maps cards when they expose prices or link to a public price
  page
- brand-specialized service public price pages

Second layer: public norm-hours / labor-time / task-duration mentions. They
are used for plausibility, not as the price basis. Professional or official
labor-time sources may be listed as future routes, but v1 uses public web only.

## Confidence

- `high`: at least 5 valid labor-only quotes, 3+ sources/regions, exact
  operation, no major missing vehicle context, plus 2+ independent public
  labor-time confirmations.
- `medium`: at least 3 valid comparable quotes and exact operation; labor-time
  may be partial or absent, but the price remains market-based.
- `low`: fewer than 3 quotes, quote confidence is weak, labor-time is absent
  for a complex job, or safety-critical context is incomplete.
- `blocked`: no exact work, no usable labor-only sample, or the request is only
  a complaint and final repair is unknown.

## Safety-Critical Work

For steering, brakes, suspension, transmission, engine, SRS, ADAS, and HV work:

- require VIN/chassis or exact vehicle context before confident final action;
- use OEM/licensed service information to verify the composition of work;
- keep price estimate separate from the technical repair decision;
- return missing context instead of guessing a final repair.

Examples:

- BMW X5 `замена рулевой рейки`: collect VIN/context, price labor-only rack
  remove/install, and mark OEM/service verification required.
- VAG DSG `замена мехатроника`: require transmission code, gearbox family, old
  unit number when relevant, and labor-only quotes without the mechatronic cost.
- `машина пинается`: price diagnostics only; do not price mechatronic,
  gearbox repair, clutch, or adaptation as final repair before diagnosis.

## CRM Output Rule

If later writing a short card summary, use only a compact result:

`Работа: замена рулевой рейки. AutoStop: 15 000 ₽. Нормо-часы: 3,5-4,0 ч, public. Уверенность: medium. Проверить: VIN/состав работы.`

If labor-time was not found, write only `Нормо-часы: публично не найдены`.
Do not add source lists, full sample details, copied public price text, or long
calculation prose into the card.
