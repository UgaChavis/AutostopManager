# AutoStop App store playbook

## Purpose

Use this route for Store catalog, stock, batches, sourcing, quote requests,
orders, warehouse and marketplace state. AutoStop App is authoritative for
these objects; CRM remains authoritative for service work and payments, and
Telegram for its dialogs.

Manager may retain only technical references, versions, hashes, cursors and
verification results. Treat request details, contacts, VINs, photos, prices,
chat text, stock rows and API payloads as transient. Never copy them into Git,
docs, Manager memory or workflow state.

## Choosing a route

Use the current named Gateway tools and their live schemas as the interface;
static examples in documentation are not an API contract.

- Use focused search and exact entity context for reads. Request full quote or
  VIN-photo detail only for one exact request and use it only in the current
  execution.
- Use the Store digest for change traversal and its opaque cursor/ACK contract.
  Health and startup reads must not advance that checkpoint.
- Use a named inventory workflow for a supported write. Internal owner
  transport, raw Store operations, browser sessions and CSRF are not user-facing
  escape hatches.
- If the needed action has no named route, stop at evidence and request a
  reviewed capability instead of improvising a write.

For part selection, combine the sources that materially reduce uncertainty:
our stock and sourcing feed, ROSSKO or contracted suppliers, Krasnoyarsk
retail, Drom/Avito and public web sources. Source order is situational. A market
listing can support a price range, but does not prove fitment or availability.

## Customer quotes

Route one exact customer quote through `store_quote_conductor`; its detailed
contract is in [store_quote_conductor_playbook.md](store_quote_conductor_playbook.md).
The customer-facing model is Admin V2 `estimate_draft`. Existing QuoteOffer
records are read only to detect a legacy or mixed case and hand it to a person;
Manager does not create, replace or repair them.

After an exact Store-to-Telegram binding is confirmed, a short preliminary
orientation may be sent through the ordinary transient `work` Telegram route
before publication. It can share candidates, a price corridor and the next
useful question, but cannot mutate Store, promise final terms or create an
order.

A final quote is saved and published in Store first. Only an independently
read-back published revision may be delivered through the conductor's typed
Telegram adapter or accepted as the context for an order. Manual or mixed
estimates, conflicting records and missing warranty or fitment evidence go to
a person without automatic cleanup.

## Writes and authority

For any supported mutation, keep the safety loop short and observable:

`exact reread -> action contract -> dry-run -> apply -> exact readback`

Use the current revision, a stable correlation ID and distinct idempotency keys
for preview and apply. A timeout, conflict or unclear response remains
unresolved until exact reread; never infer success or retry with changed input.

A request to identify or price parts does not authorize supplier purchase,
reservation, discount, payment, refund, cashbox work, deletion or changes for
another customer. Those actions need explicit authority and a suitable named
workflow. Store quote orders remain `WAITING_FOR_PAYMENT` until a person handles
payment.

## Degraded behavior

Store failure degrades only Store work. Preserve the last verified checkpoint,
report what is unknown and leave unrelated CRM work available. Never expose
credentials, human admin passwords, private transport details or raw payloads
while diagnosing the route.
