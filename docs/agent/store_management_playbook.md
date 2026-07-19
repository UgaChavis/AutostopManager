# AutoStop App store read and management playbook

## Purpose and source boundaries

Use this route for live store catalog, stock, batches, storage locations,
suppliers, quote requests, internet orders, warehouse operations, and
marketplace state. AutoStop App is the source of truth for those objects.
AutoStop CRM remains the source of truth for service cards, vehicles,
repair orders, service payments, cashboxes, files, and board state. Gmail is
the source of truth for mail. AutostopManager stores only routes, rules,
technical cursors, ActionContractV2 state, compact entity/version refs, and
verification results.

Never copy raw store orders, customer/contact data, line items, stock rows,
warehouse dumps, VIN tables, tokens, or full API payloads into Manager SQLite,
docs, Git, or workflow summaries.

## Read path

The Manager adapter calls only the internal pure-read AutoStop App API under
`/internal/agent/v1`. It never reads the store database and never uses legacy
GET routes that can synchronize, clean, commit, notify, refresh marketplaces,
or change business state.

Gateway v2 keeps exactly 24 public tools. Use the existing tools:

- `agent_bootstrap` for CRM readiness plus one compact degraded-safe Store
  snapshot; it performs one Store GET, has no cursor/ACK, and does not read the
  change feed;
- `agent_board_digest(scope="store")` for the store digest and the
  owner-visible `store_digest` stream;
- `agent_search` for bounded store lists and catalog/stock lookup;
- `agent_entity_context` for one exact store object;
- `get_runtime_status` for adapter and store health;
- `agent_inventory_workflow` for the seven allowlisted writes.

Supported entities are `store_part`, `store_order`, `store_quote_request`,
`store_supplier`, `store_batch`, `store_warehouse_operation`,
`store_marketplace_listing`, and `store_state`. `store_sourcing_offer` is a
lookup-only `agent_search` entity. Lists use opaque cursors and a strict maximum
limit. General search remains redacted; an exact `store_quote_request` with
`detail="full"` uses the dedicated quote credential and may return contacts,
VIN, request/item comments, delivery details, offers, private drafts, and
internal notes. Never persist that full payload in Manager state.

### Public command map

Select the existing Gateway command from the owner's intent; do not discover or
call a hidden Store tool directly:

| Owner intent | Public Gateway v2 call | Required arguments |
| --- | --- | --- |
| readiness, counts, stock summary, marketplace state/errors | `agent_bootstrap` | no Store cursor or ACK |
| “что нового” and change-feed traversal | `agent_board_digest` | `scope="store"`; continue with the returned opaque `cursor` and `ack_token` |
| list, filter, search, stock or sourcing candidates | `agent_search` | exact `entity`, bounded `limit`, only the filters below |
| one exact object | `agent_entity_context` | exact `entity`, `entity_id`; `detail="full"` only for an exact quote request |
| adapter/API health | `get_runtime_status` | read-only |
| one of the seven writes below | `agent_inventory_workflow` | exact `operation`, strict `payload`, unique phase `idempotency_key`, explicit `mode` |

`agent_search` entity selection and accepted filters:

| Entity | Use for | Accepted filters |
| --- | --- | --- |
| `store_part` | id, SKU, name, manufacturer; stock summary | `is_active` boolean; `low_stock` boolean |
| `store_order` | id, order number, item SKU/name | `status`: `IN_PROGRESS`, `READY`, `COMPLETED`, `ANNULLED`, or `RETURNED` |
| `store_quote_request` | id or request number | `status`: `NEW`, `IN_PROGRESS`, `PRICED`, `APPROVED`, `CONVERTED`, or `CANCELLED`; `assigned_user_id`: exact string or `null` for unassigned |
| `store_supplier` | id or supplier name | `is_active` boolean |
| `store_batch` | id, cell, part SKU/name | `status`: `IN_PROGRESS` or `COMPLETED`; exact non-empty `storage_location` up to 200 chars |
| `store_warehouse_operation` | id, receipt/shipment state | `kind`: `RECEIPT` or `SHIPMENT`; `status`: `IN_PROGRESS`, `COMPLETED`, or `ANNULLED` |
| `store_marketplace_listing` | id, external listing id, part SKU/name | `status`: `DRAFT`, `PUBLISHED`, `FAILED`, or `ARCHIVED` |
| `store_state` | aggregate Store and marketplace state | no filters; query empty or `store`, `state`, `store_state` |
| `store_sourcing_offer` | local and ROSSKO-like candidates | no filters or cursor; query is required and has at least two characters |

For marketplace export problems, use bootstrap/state aggregates for 24 hours,
7 days, all time, and the latest five safe errors. Use
`store_marketplace_listing` with `status="FAILED"` only when exact failed
listings are needed. Never request raw job messages; retry and publication are
not supported Store management commands.

For stock location, use active batches and aggregate `qty_remaining`, reserved,
and available quantities by part and storage location. Return every location;
distinguish missing location, zero physical stock, reserve-only stock, multiple
batches in one cell, one part in several cells, and external ROSSKO offers with
no local physical stock.

## What is new and cursor safety

The first read creates a baseline. Later reads return created and changed
entities. Business words such as “сегодня” and displayed report time use
`Asia/Krasnoyarsk`. App digest v3 uses a PII-free, generation-bound,
commit-ordered sequence rather than timestamps, so late commits, rollback,
child changes, tombstones, and recreate cannot be skipped. Manager
`last_success_at` is a technical UTC timestamp and every cursor is opaque;
never interpret cursor contents as business local time.

Manager digest reads one page per call:

1. Without a caller cursor, read from the committed checkpoint. If a page was
   delivered but not acknowledged, replay it from App `replay_cursor` with the
   original limit and fixed window; do not advance anything.
2. Every non-empty page, including the final source page, returns only a
   Manager-owned opaque `page.next_cursor`, `page.ack_token`, and
   `page.ack_required=true`. Raw App checkpoint/replay cursors are never copied
   into the public summary.
3. Pass that exact cursor/token pair on the next call. An intermediate ACK uses
   compare-and-swap to advance only traversal state and immediately returns the
   next page. A final ACK atomically commits the high-water and returns an empty
   terminal response with `page.next_cursor=null`.
4. Lost responses are idempotent: an unacknowledged page replays with the same
   delivery identity, and a repeated final ACK returns the same terminal
   response. Replay membership, source cursor, source has-more, original limit,
   and compact page refs must match or Manager fails closed.
5. A timeout, malformed DTO, generation mismatch, replay mismatch, CAS
   conflict, or store outage records degraded/conflict state but preserves the
   committed checkpoint and pending traversal. Use the scoped
   `store-checkpoint-reset --stream ... --confirm-rebaseline` only after an
   explicit reset decision; never silently discard a generation mismatch.

Bootstrap uses stateless `/bootstrap-snapshot`, never `store_digest` or the
legacy `store_bootstrap` checkpoint, so startup cannot hide changes from “Что
нового появилось в магазине?”. One response contains Store API readiness,
product/active-order/open-request counts, aggregate stock, marketplace state,
safe marketplace export-error counts for 24 hours/7 days/all time, the latest
five generic errors, and Store contract version. Error entries contain only
date, fixed `error_code`, part/account refs, and attempt count; raw messages,
payloads, tokens, and contacts are forbidden. `agent_board_digest(scope="store")`
alone uses `cursor` and `ack_token`.

## Minimal write allowlist

Only these domain/action pairs are allowed:

- `store_quote_request/assign_quote_request` with `assignee_id`;
- `store_quote_request/set_quote_request_status` with `NEW` or `IN_PROGRESS`;
- `store_quote_request/update_quote_request_comment` with `internal_comment`;
- `store_quote_request/add_quote_request_note` with append-only `text`;
- `store_quote_request/replace_quote_offer_drafts` with at most three private
  structured drafts per exact quote item;
- `store_batch/set_batch_storage_location` with `storage_location`;
- `store_order/mark_order_ready` with explicit `status=READY`, only from
  `IN_PROGRESS` and only on an exact owner command.

### Write command selector

Every public write is
`agent_inventory_workflow(operation=<action>, payload=<payload>,
idempotency_key=<unique phase key>, mode="dry_run"|"apply")`. The payload
always contains `target_id`, current timezone-aware `expected_updated_at`, the
task-specific `owner_intent`, strict `planned_changes`, and normally one stable
`correlation_id` shared by dry-run and apply.

| Owner command | Domain / operation | Exact `planned_changes` | Exact reread check |
| --- | --- | --- | --- |
| assign the quote request | `store_quote_request/assign_quote_request` | `{"assignee_id":"<non-empty id, max 36>"}` | assigned user only |
| move the quote request to work or back to new | `store_quote_request/set_quote_request_status` | `{"status":"IN_PROGRESS"}` or `{"status":"NEW"}` | status only |
| replace or clear the current internal comment | `store_quote_request/update_quote_request_comment` | `{"internal_comment":"<max 2000>"}` or `{"internal_comment":null}` | replaceable comment only |
| append a note/history entry without replacing prior text | `store_quote_request/add_quote_request_note` | `{"text":"<non-empty, max 2000>"}` | one new append-only note |
| replace the complete private offer-draft set for one exact quote | `store_quote_request/replace_quote_offer_drafts` | `{"items":[...]}` using the draft rules below | complete Manager-owned draft post-state for that quote |
| change one batch cell/location | `store_batch/set_batch_storage_location` | `{"storage_location":"<non-empty, max 200>"}` | storage location only |
| mark one assembled order ready | `store_order/mark_order_ready` | `{"status":"READY"}` | status and disclosed notification result |

Choose `update_quote_request_comment` for “обнови/замени/очисти внутренний
комментарий”. Choose append-only `add_quote_request_note` for “добавь заметку”
or “добавь запись в историю”. Do not guess when the owner wording does not say
whether existing text must be replaced or preserved.

For `replace_quote_offer_drafts`, first read the full exact quote and send the
complete desired post-state of Manager-owned drafts for that quote. Existing
Manager drafts for the same agent that are omitted from `items` are superseded,
not preserved. `items` has 1..20 unique exact quote item ids and each item has
0..3 drafts with unique `candidate_key`; at most one draft per item is
recommended. Each draft requires non-empty `candidate_key` and
`part_name`, positive `sale_price`, `source_kind` from
`LOCAL|ROSSKO|CATALOG|WEB|MANUAL_REFERENCE`, and `price_basis` from
`STORE_RETAIL|CONFIRMED_PURCHASE|PUBLIC_RETAIL|ESTIMATE`. Optional fields are
`part_sku`, `brand`, `supplier`, positive `purchase_price`, `delivery_days`
0..3650, `comment` up to 1000, `source_ref` up to 500, `source_url` up to 1000,
`availability` up to 300, `fitment_confidence` from
`HIGH|MEDIUM|LOW|UNVERIFIED`, `oem_reference` up to 120, and
`is_recommended`. To clear the complete Manager-owned draft set, send at least
one valid exact quote item with an empty `drafts` list. The operation never
changes a published offer or another principal's drafts.

For every write: reread the exact target, build `prepare_action_contract`, pass
`expected_updated_at`, and call `agent_inventory_workflow` in `dry_run`. The
dry-run and apply phases must use two distinct unique idempotency keys and the
same stable correlation ID. ActionContractV2 derives that correlation from the
normalized domain, action, target, revision, and planned changes; an explicit
correlation is optional and must be an alphanumeric-first 8..160 character safe
identifier. Owner wording, mode, and phase idempotency keys must not change it.

Dry-run intentionally records a sanitized receipt and audit proof while leaving
business state and `updated_at` unchanged. Inspect its effects, then apply only
with a matching proof that is at most 1800 seconds old. Direct apply without a
fresh matching proof is blocked. A READY dry-run must disclose whether a
customer notification will be sent. After apply, reread the exact target.

For lost-response recovery, if the apply pre-read already shows a newer
revision, resend the original apply request with its original
`expected_updated_at`, idempotency key, payload, and correlation so the App can
replay the receipt before its stale-revision check. Without a matching receipt,
the App returns a conflict. POST is never retried automatically. A POST timeout,
network error, 5xx, oversized response, invalid JSON, or invalid response schema
has an uncertain outcome: always perform one exact reread and keep the result in
`compensating` with `write_applied_unverified` evidence until reconciliation.
A successful idempotent replay reports `meta.idempotency_replay=true` and may
return the already-applied result without advancing `updated_at` again.

Store workflow ledger rows are refs-only. Gateway workflow IDs such as
`inventory:<operation>` are store workflows even without a `store_*` scope.
Keep `query` empty and use only allowlisted machine intent, summary, message,
event, compact-ref, count, status, hash, cursor, and version values; never put
owner prose, customer/contact data, raw payloads, or secrets in those channels.

Never expose generic hidden Manager store tools through raw discovery. Never
allow deletion, price/product/customer/quantity/finance changes,
COMPLETE/ANNULLED/RETURNED, ROSSKO ordering, marketplace publication, mass
changes, messages, or arbitrary settings.

## Runtime configuration and degraded behavior

The composition root injects only `AUTOSTOP_STORE_API_URL`,
`AUTOSTOP_STORE_READ_TOKEN`, `AUTOSTOP_STORE_QUOTE_TOKEN`, and
`AUTOSTOP_STORE_MANAGE_TOKEN`. Tokens are
runtime secrets; do not print their values or use a human ADMIN password. The
URL is fail-closed: production accepts only
`http://autostop-app:8000/internal/agent/v1` and local tests may use an explicit
loopback port; userinfo, query, fragment, other paths, and external hosts are
rejected. The
adapter uses separate read/quote/manage identities, bounded responses, schema
validation, redaction, timeout, GET-only retries, and a circuit breaker.
Authentication and request conflicts do not trip the outage circuit.

Store failure degrades only store fields in bootstrap/runtime/digest. CRM
operations remain available.

## Routing boundaries

“Магазин”, “наш каталог”, store stock/location, quote requests, internet
orders, warehouse movements, low stock, and marketplace export errors use this
playbook. General public sourcing such as “найди деталь на Drom/Avito” stays in
`parts_search_playbook.md`. “Заказ-наряд”, service materials, service debt, and
service payments stay in CRM workflows.

## Verification

- Manager raw registry contains the Store INTERNAL_ONLY tools, while public
  Gateway v2 remains exactly 24 tools.
- Read API calls never mutate store state; read-only credentials cannot write.
- Bootstrap is one stateless Store request with no ACK/change-feed query;
  digest pagination, ACK/replay, abort/resume, CAS conflict, and failure
  preservation tests pass unchanged.
- Seven writes pass DTO-shaped dry-run/apply/idempotency/concurrency/readback
  tests, including two-change comment/READY responses, stale receipt replay,
  and uncertain-outcome reconciliation; production smoke is read-only plus safe
  dry-run.
- No raw store payload or secret appears in Manager SQLite, logs, docs, or Git.
- CRM, store, public site, Gateway, and VPN health remain green after deploy.
