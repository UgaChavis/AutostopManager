# Store Analytics Reporting

Canonical read-only route for first-party storefront statistics from AutoStop
App. The reporting interface is AutostopManager; there is no separate analytics
application or admin panel.

## Source and Tool

- Source of truth: aggregate endpoint
  `POST /internal/agent/v1/analytics/report` in AutoStop App.
- Hidden Manager capability: `get_store_analytics_report`.
- Public access path: `discover_raw_capabilities` ->
  `get_raw_capability_schema` -> `call_raw_capability` because this is a pure
  read with no named public workflow.
- Timezone: `Asia/Krasnoyarsk` for day boundaries and owner-facing periods.
- Public Gateway v2 surface remains exactly 24 tools; the hidden Manager raw
  registry has 73 tools after Store management, analytics, guarded owner
  API integration.

The runtime reuses the AutoStop App `store:read` service identity through
`AUTOSTOP_STORE_READ_TOKEN`; the report is DB-backed and does not call the
public storefront. Production accepts only
`http://autostop-app:8000/internal/agent/v1`; tests may use an explicit
loopback port. Public/external hosts, userinfo, query, fragment, and other paths
are rejected. The client never follows redirects, so the bearer cannot cross
an origin boundary. Never print or persist the token outside runtime `.env`.

## Natural Queries

Pass the owner query unchanged in `query`; the tool resolves `today`,
`yesterday`, `last_7_days`, or `last_30_days` and returns a concise Russian
answer. Supported direct routes include:

- `сколько посетителей сегодня`;
- `какие товары смотрели за неделю`;
- `куда чаще нажимают`;
- `сколько времени проводят на сайте`;
- `какая конверсия в корзину и заказ`;
- `статистика магазина` and `аналитика сайта`.

For custom periods use inclusive `date_from` and `date_to` in `YYYY-MM-DD`;
the client sends Krasnoyarsk midnight boundaries and caps the range at 62
days. Compare the current period with the previous equal-duration period.

## Metric Contract

- Visitors: distinct pseudonymous `visitor_id` values in the period.
- Sessions: distinct 30-minute inactivity sessions in the period.
- Page views: semantic SPA page-view events, including `/search`.
- Active time: sum of visible-tab engagement heartbeats per session; report
  both mean and median seconds and the count of measured sessions.
- Popular pages: page views by normalized path without query or fragment.
- Popular products: product views with authoritative catalog name/SKU when
  available; external tokens are replaced with a short hash while brand/SKU
  are decoded locally, and cart additions are supplemental.
- Search quality: search count, zero-result count, and zero-result rate. Exact
  search text is never available.
- Interactions: cart additions, confirmed quote submissions, confirmed orders,
  and explicitly marked meaningful clicks.
- Funnel: session sequence `product_view -> add_to_cart -> quote/order`; an
  event occurring out of order does not advance the funnel.

State the period and comparison when giving a result. Do not infer business
causality from small counts, and distinguish visible-tab time from total visit
duration.

## Privacy Boundary

Only aggregate output may reach the agent. Reject or omit any response that
contains raw events, `visitor_id`, `session_id`, IP, User-Agent, exact search
text, form values, customer/account links, name, phone, email, VIN, or click
coordinates. Do not copy the report into durable Manager memory; it is current
operational data.

The browser notice describes the identifier as random and pseudonymous, not
fully anonymous. Opt-out deletes browser identifiers and stops future sends;
already collected raw events expire through the approximately 60-day
retention job. Application and backup retention must be reviewed together.

This technical design is not a declaration of legal compliance. The owner must
validate the operator identity and public policy, the documented processing
basis and balancing/consent record, the Roskomnadzor notification, localization,
data-subject request handling, and security measures. If consent is chosen as
the basis, account for the separate-consent requirement effective 2025-09-01.

Official references reviewed for this route:

- 152-FZ consolidated official text:
  `https://ips.pravo.gov.ru/api/ips/legislation/document?baseid=None&hash=98490812b3409e2a8d78a11ca9010f434ea3d9250a11dbbdb78690cd5551bdd6`;
- 156-FZ of 2025-06-24 (separate consent):
  `https://publication.pravo.gov.ru/document/0001202506240021`;
- 23-FZ of 2025-02-28 (localization changes):
  `https://publication.pravo.gov.ru/document/0001202502280034`;
- 420-FZ of 2024-11-30 (administrative liability):
  `https://publication.pravo.gov.ru/document/0001202411300011`;
- FSTEC Order 21 security measures:
  `https://minjust.consultant.ru/documents/6146?items=10`.

## Verification

1. Confirm the report format is `store_analytics_report_v1`, timezone is
   `Asia/Krasnoyarsk`, and `meta.aggregatedOnly=true` with
   `rawEventsIncluded=false`.
2. Confirm requested and previous periods are equal in duration.
3. Confirm no forbidden raw/private keys occur anywhere in the output.
4. For production smoke, send one safe test event through the public ingest,
   call this capability through Gateway raw discovery, and find the aggregate
   change without exposing the event record.
5. Recheck public Gateway tool count is exactly 24 and CRM/store health remains
   green.
