# AutoStop App store read and management playbook

## Purpose and source boundaries

Use this route for live store catalog, stock, batches, storage locations,
supplier-sourcing evidence, quote requests, internet orders, warehouse
operations, and marketplace state. AutoStop App is the source of truth for
those objects.
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

- `agent_bootstrap` for CRM/Manager startup; Store is intentionally returned as
  `not_loaded`, so follow with `get_runtime_status` or a focused Store call;
- `agent_board_digest(scope="store")` for the store digest and the
  owner-visible `store_digest` stream;
- `agent_search` for bounded store lists and catalog/stock lookup;
- `agent_entity_context` for one exact store object; an exact quote may use
  `detail="full_with_vin_photo"` to read only safe VIN-photo metadata;
- `get_runtime_status` for adapter and store health;
- `agent_inventory_workflow` for reviewed named writes, including the typed
  `store_quote_conductor` for Admin V2 customer estimates.

`store_owner_capabilities` and `store_owner_api` are Manager-internal transport
components, not public Gateway tools.  Raw discovery and raw invocation reject
them; a caller cannot select an arbitrary Store operation.  A new public Store
action needs its own reviewed named workflow and release.

Supported entities are `store_part`, `store_order`, `store_quote_request`,
`store_batch`, `store_warehouse_operation`,
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
| CRM/Manager startup and route selection | `agent_bootstrap` | Store remains `not_loaded`; no Store cursor or ACK |
| Store readiness, counts, stock summary, marketplace state/errors | `get_runtime_status` or `agent_search` | use `entity="store_state"` for the focused state row |
| “что нового” and change-feed traversal | `agent_board_digest` | `scope="store"`; continue with the returned opaque `cursor` and `ack_token` |
| list, filter, search, stock or sourcing candidates | `agent_search` | exact `entity`, bounded `limit`, only the filters below |
| one exact object | `agent_entity_context` | exact `entity`, `entity_id`; `detail="full"` or `"full_with_vin_photo"` only for an exact quote request |
| one exact VIN-photo preview | `agent_document_workflow` | `operation="download_store_quote_vin_photo"`, exact quote id, current photo SHA-256, idempotency key, and `allow_large_output=true` |
| adapter/API health | `get_runtime_status` | read-only |
| one of the seven optimized writes below | `agent_inventory_workflow` | exact `operation`, strict `payload`, unique phase `idempotency_key`, explicit `mode` |
| an operation without a named workflow | stop and request a reviewed workflow/release | do not use raw owner API or browser/CSRF access |

### Service materials and supplier purchases

A service-card request to buy oil or a filter authorizes identification,
stock/sourcing reads, and a verified material list only. Never substitute a
customer `SalesOrder`, `create_manual_order`, or ROSSKO confirmation for
workshop procurement. Execute a supplier purchase only when the live OpenAPI
advertises a dedicated supplier operation and the owner separately directs
that exact purchase. Otherwise return
`supplier_order_capability_unavailable`; this is a safe terminal result. The
current Store has no supplier-directory entity or supplier CRUD. Supplier names
exist only in quote offers, order source labels, ROSSKO sourcing, and masked
ROSSKO configuration.

`agent_search` entity selection and accepted filters:

| Entity | Use for | Accepted filters |
| --- | --- | --- |
| `store_part` | id, SKU, name, manufacturer; stock summary | `is_active` boolean; `low_stock` boolean |
| `store_order` | id, order number, item SKU/name | `status`: `WAITING_FOR_PAYMENT`, `IN_PROGRESS`, `IN_TRANSIT`, `READY`, `COMPLETED`, `ANNULLED`, or `RETURNED` |
| `store_quote_request` | id or request number | `status`: `WAITING_FOR_QUOTE`, `WAITING_FOR_APPROVAL`, `APPROVED`, or `CANCELLED_BY_CUSTOMER`; `assigned_user_id`: exact string or `null` for unassigned |
| `store_batch` | id, cell, part SKU/name | `status`: `IN_PROGRESS` or `COMPLETED`; exact non-empty `storage_location` up to 200 chars |
| `store_warehouse_operation` | id, receipt/shipment state | `kind`: `RECEIPT` or `SHIPMENT`; `status`: `IN_PROGRESS`, `COMPLETED`, or `ANNULLED` |
| `store_marketplace_listing` | id, external listing id, part SKU/name | `status`: `DRAFT`, `PUBLISHED`, `FAILED`, or `ARCHIVED` |
| `store_state` | aggregate Store and marketplace state | no filters; query empty or `store`, `state`, `store_state` |
| `store_sourcing_offer` | local and ROSSKO-like candidates | no filters or cursor; query is required and has at least two characters |

### Exact full-information reads

“Full information” means a focused read of the exact object and all fields
needed for the current task, never an unbounded customer, order, or warehouse
export. Resolve with a redacted named read, then use only an exact operation
present in the live OpenAPI when the named DTO is insufficient. Prefer
`agent_entity_context(detail="full")` for a quote request; keep customer PII and
money transient. Store settings have an exact read, but static pages, banners,
menus and design have no CMS API and require a separate Store code release.

For orders, prefer the bounded page and exact-order operations. Send
`itemLimit` on the first read, then the returned `itemsNextCursor` with the same
limit. Each success reports
`itemsTotal/itemsLimit/itemsHasMore/itemsNextCursor`; continue only while
`itemsHasMore=true`. On `ORDER_ITEMS_CURSOR_STALE`, restart that exact order
without a cursor. If the bounded operations are absent from live OpenAPI, the
unpaginated fallback requires one exact `order_id` and is never a bulk export.
An unpaginated employee list and the transport's 2 MiB ceiling are not proof of
completeness.

### Admin V2 estimate conductor

For a quote led by `store_quote_conductor`, Admin V2 `estimate_draft` is the
only customer-facing quote model.  Do not use the legacy Manager
`replace_quote_offer_drafts` path for that request: a QuoteOffer conflicts with
the existing estimate-to-order path.

The conductor uses the dedicated Store owner operations to read, replace,
submit, reopen and confirm an Admin V2 estimate.  They are action routes, not
browser endpoints: use the current owner schema, exact revision,
ActionContractV2, dry-run, apply with a different idempotency key and exact
readback.  A manual estimate or a legacy mixed estimate/offers record is a
handoff, never an automatic cleanup.

On submission, Store publishes the estimate to the customer cabinet. Only after
Store readback may the same exact request receive a bounded work Telegram
message. Telegram consent is accepted only from a typed adapter's opaque inbound
receipt and independent readback proving the same private peer and delivery
binding; caller-supplied consent/context hashes are not proof. Current consent
converges with cabinet confirmation on one `WAITING_FOR_PAYMENT` order. It does
not authorize a reserve, supplier purchase, discount, payment, cashbox entry or
card details.

See `store_quote_conductor_playbook.md` for the state machine, conversational
rules and recovery contract.

### Quote compatibility

For a new request, `store_quote_conductor` and Admin V2 `estimate_draft` are
the only customer-facing path. A preliminary Telegram orientation is allowed
before publication but never replaces it. Publication requires complete current
evidence and Store readback; only then can a typed adapter deliver the summary
and reconcile one `WAITING_FOR_PAYMENT` order.

`QuoteOffer` is legacy guardrail, not a workflow: a manual, mixed or existing
legacy record goes to handoff. Never repair it, invoke raw owner operations, or
move it automatically between models. The exact projection must positively
exclude an Admin V2 estimate before any retained legacy guard is considered.

For marketplace export problems, use `store_state` aggregates for 24 hours,
7 days, all time, and the latest five safe errors. Use
`store_marketplace_listing` with `status="FAILED"` only when exact failed
listings are needed. Never request raw job messages. Retry and publication are
not supported by a named workflow; treat a request for them as a separate
reviewed implementation task, not a raw owner-API call.

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

`agent_bootstrap` does not load Store and cannot advance a Store checkpoint.
`get_runtime_status` and focused `store_state` reads are stateless; only
`agent_board_digest(scope="store")` uses `cursor` and `ack_token`. Thus startup
and health checks cannot hide changes from “Что нового появилось в магазине?”.

## Internal owner transport boundary

The `autostop-manager-owner` principal and `store_owner_api` are implementation
details used only inside reviewed typed Manager workflows. They are neither
discoverable nor callable as public Gateway capabilities. A user request must
never turn into a raw operation ID, browser session or CSRF flow; if a named
workflow does not support it, stop for a reviewed implementation/release.

The typed conductor is the only internal consumer of the dedicated Admin V2
estimate operations. It uses current Store schema/plan checks, exact revision,
ActionContractV2, server-issued dry-run proof, distinct idempotency keys,
correlation ID and exact readback. The durable ledger contains only hashes,
counts, status and version references. Unknown or conflicting outcomes remain
compensating until reread; no HTTP response alone proves an effect was absent.

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
| change only the quote workflow status (never substitute for publishing a response) | `store_quote_request/set_quote_request_status` | `{"status":"WAITING_FOR_QUOTE"}` or `{"status":"WAITING_FOR_APPROVAL"}` | status only |
| replace or clear the current internal comment | `store_quote_request/update_quote_request_comment` | `{"internal_comment":"<max 2000>"}` or `{"internal_comment":null}` | replaceable comment only |
| append a note/history entry without replacing prior text | `store_quote_request/add_quote_request_note` | `{"text":"<non-empty, max 2000>"}` | one new append-only note |
| legacy QuoteOffer repair after human handoff only | `store_quote_request/replace_quote_offer_drafts` | never a new customer quote; do not route ordinary wording here | handoff evidence, not publication |
| change one batch cell/location | `store_batch/set_batch_storage_location` | `{"storage_location":"<non-empty, max 200>"}` | storage location only |
| mark one assembled order ready | `store_order/mark_order_ready` | `{"status":"READY"}` | status and disclosed notification result |

Choose `update_quote_request_comment` for “обнови/замени/очисти внутренний
комментарий”. Choose append-only `add_quote_request_note` for “добавь заметку”
or “добавь запись в историю”. A bare “добавь комментарий” is ambiguous between
an internal note and a customer-visible offer comment: clarify its destination.
Do not guess when the owner wording does not say whether existing text must be
replaced or preserved.

`replace_quote_offer_drafts` is retained solely for an existing legacy case
that a person has already taken over. It is never selected by a normal quote
phrase and cannot coexist with Admin V2 `estimate_draft`; a manual or mixed
record remains a handoff rather than an automatic repair.

For every named write, reread the exact target, build
`prepare_action_contract`, dry-run with `expected_updated_at`, then apply with a
different idempotency key, the same stable correlation ID and a fresh matching
proof. A READY dry-run must disclose customer notification. Reread after apply.
On a lost response, replay only the identical original request; never retry a
POST automatically with new inputs. Timeout, network/5xx, oversized or invalid
response means uncertain outcome: exact reread is mandatory and the result
stays `compensating` until reconciled.

Store workflow ledger rows are refs-only. Gateway workflow IDs such as
`inventory:<operation>` are store workflows even without a `store_*` scope.
Keep `query` empty and use only allowlisted machine intent, summary, message,
event, compact-ref, count, status, hash, cursor, and version values; never put
owner prose, customer/contact data, raw payloads, or secrets in those channels.

### VIN-photo preview

`full_with_vin_photo` is available only for one exact `store_quote_request` and
uses the dedicated quote credential. It returns `vin_photo=null` or metadata
(`sha256`, JPEG MIME type, byte size, width, height), never the filename, URL,
or bytes. For a present photo, call `agent_document_workflow` with
`download_store_quote_vin_photo`, the exact id and SHA-256, a unique idempotency
key, and `allow_large_output=true`. Gateway returns an in-memory bounded JPEG
preview as ImageContent; bytes must never be copied to Manager state, logs,
docs, Git, or workflow ledger. A stale hash, absent photo, non-JPEG response,
or oversized preview fails closed. This is read-only: it does not perform OCR,
write a VIN, or change the quote.

## Runtime configuration and degraded behavior

Runtime injects a fail-closed internal Store URL and separate
read/quote/manage/owner credentials. The owner credential is only for the
reserved `store:owner` service principal; never print tokens or substitute a
human ADMIN password. The adapter bounds and validates responses, retries only
GET, and isolates Store outages with a circuit breaker.

Store failure degrades only Store runtime/search/digest fields. CRM
operations remain available.

## Routing boundaries

“Магазин”, “наш каталог”, store stock/location, quote requests, internet
orders, warehouse movements, low stock, and marketplace export errors use this
playbook and `$manage-autostop-store`. General public sourcing such as “найди
деталь на Drom/Avito” stays in `parts_search_playbook.md`. “Заказ-наряд”, service
materials, service debt, and service payments stay in CRM workflows.

## Verification

- Manager raw registry contains 78 tools, including guarded
  `store_owner_capabilities` and `store_owner_api`; public Gateway v2 remains
  exactly 24 tools.
- Read API calls never mutate store state; read-only credentials cannot write.
- Bootstrap does not call Store; runtime health and focused reads are stateless.
  Digest pagination, ACK/replay, abort/resume, CAS conflict, and failure
  preservation tests pass unchanged.
- Seven optimized named writes pass DTO-shaped
  dry-run/apply/idempotency/concurrency/readback tests. The owner capability
  matrix additionally proves every non-session employee route is reachable by
  only `store:owner`; generic applies require schema-bound request/response
  validation, backend revision/receipt enforcement, and remain compensating
  until operation-specific reread. Production smoke is read-only plus safe
  server-side dry-run.
- No raw store payload or secret appears in Manager SQLite, logs, docs, or Git.
- CRM, store, public site, Gateway, and VPN health remain green after deploy.
