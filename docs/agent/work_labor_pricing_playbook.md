# Work Labor Pricing Playbook

Purpose: estimate AutoStop labor price by reconciling the exact vehicle/work
scope, aggregate-only experience from closed AutoStop repair orders, current
public labor-only market prices, and AUTONORMS/OEM/professional labor-time
evidence. No one source is the answer by itself. This route is read-only: it
prepares a manager estimate but does not write repair-order works, materials,
prices, payments, or cashbox data.

## Trigger

Use this playbook when the owner asks:

- сколько стоит работа
- оценить стоимость работ
- смета по работам
- работа без запчастей
- средняя цена по России плюс 50%
- нормо-часы по работе
- трудоемкость ремонта

On Gateway v2, discover the raw capability, read its schema, then call
`estimate_repair_work_cost`. For local manager use:

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
4. Read the aggregate internal snapshot
   `data/private_knowledge/service_pricing_experience.json`. Match the exact
   operation and use its median/IQR, sample size, vehicle segment, and
   freshness as a historical anchor. Never treat it as a current price list.
5. Collect current public Russia STO prices for the same operation and comparable
   vehicle class. Keep only labor-only prices. Do not mix work prices with
   parts, fluids, programming licenses, towing, or aggregates.
6. Resolve vehicle/operation labor time through AUTONORMS when configured,
   then use public/professional service evidence as additional plausibility.
   Search terms include `нормо-часы`, `норма
   времени`, `трудоемкость`, `время выполнения`, and `снять/установить`. Use
   licensed sources only through legal configured access; never bypass login,
   CAPTCHA, paywalls, or copy licensed tables.
7. Store a short market sample only: source, city/region, operation name,
   price, `includes_parts=false`, capture date, and quote confidence. Do not
   store full HTML pages, full price lists, or raw screenshots in memory/CRM.
8. Store a short labor-time sample only: source, operation, hours/range, capture
   date, confidence, and whether the source is public. Do not call it official
   OEM norm-hours unless the public source itself is an official open source.
9. Exclude obvious outliers. Keep the legacy public benchmark:
   `russia_average_rub = arithmetic_mean(valid_public_quotes_after_outlier_filter)`.
10. Keep the legacy public-only AutoStop benchmark:
   `autostop_price_rub = round_to_100(russia_average_rub * 1.50)`.
11. Build `recommended_price_rub` by reconciling the internal anchor and
    current market. Use labor time for effective-rate, overlap, and scope
    checks. If evidence diverges materially, show `recommended_range_rub`,
    explain why, and lower `decision_confidence`.
12. High confidence requires exact vehicle/work context and at least three
    independent evidence families. Internal experience alone is `low`, even
    with many observations. Fewer than three public quotes no longer blocks a
    provisional internal estimate, but it blocks a current market-confirmed one.
13. Before a repair-order write, require a separate explicit owner command and a
   live CRM target. This tool must not call `replace_repair_order_works`.

## Internal AutoStop Experience

Refresh on direct owner request or an intentional pricing review:

```bash
.venv/bin/python -m autostop_manager.cli service-pricing-refresh \
  --state-json /opt/autostopcrm/data/state.json \
  --limit 100
```

The snapshot is aggregate-only and private. It contains no order ids, clients,
phones, VINs, plates, payments, or raw order rows. Labor baselines use unit
work-row prices before separately displayed order tax and report sample count,
median, P25/P75, min/max, freshness, and coarse vehicle segment. Article price
references require a catalog number and remain historical until live Store,
supplier, public market, and applicability checks pass.

## PartsAPI AUTONORMS Layer

When the vehicle and work are specific enough, use AUTONORMS as the preferred
configured labor-time evidence layer before broad public-web plausibility
searches. It is not a price-list source and does not replace the public Russia
labor-only market sample.

1. If only a Russian registration number is available, use the read-only
   `plate_to_vin` route first and verify any returned VIN against the vehicle
   and CRM; do not write it automatically.
2. Resolve the full AUTONORMS chain with bounded one-method calls:
   `norms_makes` -> `norms_models(make_name_seo)` ->
   `norms_motors(model_id)` ->
   `norms_times(motor_id, top_category_id, sub_category_id)`.
3. Choose the category from the public AUTONORMS category source referenced in
   `partsapi_method_contracts.md`; retain only the selected category/work rows,
   never a copied database dump.
4. Match returned `workName`/`workTarget` to the requested operation. Account
   for overlap: do not simply sum rows that contain the same remove/install
   work.
5. Use `workTime` only for plausibility, scope comparison, and effective hourly
   rate. Do not treat provider `workPrice` (often zero) as the AutoStop price.
   Final price remains `round_to_100(russia_average_rub * 1.50)` when the
   market sample is sufficient.
6. Demo keys are method-specific and quota-limited. Make the minimum number of
   calls, use `max_attempts=1` by default, and return `provider_unavailable` or
   `inconclusive` for provider HTTP 5xx/empty data rather than inventing hours.

For brand/article wording, use `part_name_by_brand_number`; for localized
replacement descriptions, use `crosses_title`. Both are article enrichment and
never fitment proof or a substitute for VIN-specific OEM evidence.

## Source Basis

Evidence families: internal closed-order aggregate, current public STO market,
vehicle-specific labor time/service data, and exact live vehicle/scope context.

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

- `high`: exact vehicle and operation, three independent evidence families,
  and no material unresolved conflict.
- `medium`: two independent families with exact enough scope, or a three-family
  estimate with a documented conflict/range.
- `low`: one family only, anecdotal/old internal data, weak market evidence,
  absent labor-time data for a complex job, or incomplete safety context.
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

If later writing a short public card summary, include only confirmed working
facts, for example: `Работа: замена рулевой рейки — 15 000 ₽.` Keep confidence,
sources, public norm-hour status, missing context, and required checks in the
internal estimate/owner report. Do not add them, copied price text, or long
calculation prose to the public card.
