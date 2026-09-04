# Store quote conductor

`store_quote_conductor` is the only Manager workflow allowed to lead an
Admin V2 client quote from clarification through an unpaid Store order.  It is
not a browser automation route and never borrows an Admin V2 session or CSRF
token.

## Canonical data and boundaries

- AutoStop App owns the request, Admin V2 `estimate_draft`, publication,
  customer cabinet and SalesOrder.  Telegram owns the dialog.
- The canonical customer quote is an Admin V2 estimate.  Do not create or
  publish `QuoteOffer` / Manager drafts for the same request.
- Manager keeps only quote IDs, revisions, snapshot/context hashes, workflow
  states and verification results.  It never persists chat text, contact data,
  VIN, client prices, supplier evidence or payment data.
- The named conductor exclusively owns its workflow ledger. Generic workflow
  checkpoint, transition, external-step, resume and compatibility paths reject
  its run; internal records accept only fixed status, aggregate count and
  one-way technical-reference fields.
- A quote may contain a position mapped to an original request item or a
  `conversation_added` position.  The latter needs a current confirmed dialog
  context and the same fitment, availability, price, term and warranty proof.

## State machine

`new -> clarifying -> evidence_ready -> draft_saved -> published ->
waiting_client -> waiting_payment`

Alternate terminal or intervention states are `revision_needed`, `handoff` and
`declined`.  There is one active run per Store request.  Every transition uses
the current state version; stale transitions fail rather than overwriting a
newer run.

Before a customer-visible Store mutation, reread the exact request and its
revision, prepare the action contract, run Store dry-run, apply with a new
idempotency key, and independently reread.  A timeout or partial result stays
compensating until it is reconciled.

## Eligibility and escalation

An automatic recommendation is allowed only when the exact part/application,
article, availability, lead time, client price and service warranty are all
confirmed.  Show at most three understandable choices: an original option if
available and up to two verified analogues.

Stop and hand off for uncertain fitment, conflicting catalogue evidence,
unconfirmed availability or warranty, a requested discount, supplier purchase,
reservation, unusual delivery, payment confirmation, or manual Store estimate.
The Store-owned default warranty text must be configured before an estimate can
be submitted; Manager does not invent it.

## Telegram dialog

Use the `work` account and one exact private peer resolved from the current
Store request.  A unique route is not proof of identity: without a confirmed
Store-Telegram binding, a privileged setup exchange may send only the literal
neutral question `Привет! Вы оставляли заявку на запчасти в AutoStop?`.  It
creates a pending stable route binding from the quote reference plus the exact
published revision, snapshot and context; a later Store revision/snapshot
cannot reuse the old peer, and a caller boolean cannot mark it confirmed.  The
typed bridge mints and independently rereads one opaque direct-reply receipt.
Only bare `да` in this identity exchange promotes the route; `нет` and
ambiguous text do not.  A normal offer can bind only after that promotion.
Identity setup is outside the conductor: `identity_prompt` is explicitly
rejected as a published quote delivery and can never be classified as order
consent.

Each sent message is bound to the quote ID, published estimate revision and
context hash.  The text is transient; the ledger keeps only its hash.  Write
one to three short, natural sentences with one next question.  Do not disclose
internal sources, purchase prices, automation details, payment requisites or
unconfirmed facts.

The Manager MCP server intentionally starts without a live sender.  A pilot
must inject a concrete typed `work`-Telegram adapter that performs dry-run,
idempotent apply and independent delivery readback, and proves one confirmed
private target plus the quote/revision/snapshot/context binding hashes.  Until
that adapter is deployed, publication may be prepared but the conductor fails
closed before any Telegram send.

An inbound client reply is accepted only through a separate opaque transport
receipt. The adapter independently rereads that receipt, proves the same
confirmed private work peer and exact delivered-message binding, and returns
only its classification and hashes. A caller may not supply consent, snapshot,
revision, context hash, peer or classification as proof; missing, stale or
mismatched inbound readback fails closed.

Only an unambiguous consent attached to the current published context can
create an order.  A question, an old reply, a different quote, or an ambiguous
"да" must not create an order.

## Payment and recovery

Telegram consent and cabinet confirmation converge on one Store
`WAITING_FOR_PAYMENT` order.  The workflow never reserves stock, buys from a
supplier, applies a discount, records payment, changes a cashbox or sends card
details.  It may say that payment is available at reception or by an approved
employee instruction.

Store is published first, then Telegram is sent.  If Telegram delivery cannot
be verified, keep the Store publication intact and resume the same workflow;
never republish the estimate or duplicate the order.

While waiting for Telegram, `status` rereads the Store request.  If the client
has already confirmed in the cabinet and Store exposes `convertedOrderId`, it
records that single existing order as `waiting_payment`, completes the pending
Telegram wait and does not send or create anything else.

## Learning and pilot controls

At completion record only a de-identified technical review: outcome,
verification state, route version and improvement category.  A proposed rule,
template or source change requires owner review before promotion.

Release in fixed waves:

1. Synthetic, de-identified end-to-end cases with no Store or Telegram write.
2. Ten owner-selected pilot requests: the owner confirms publication and the
   first Telegram message; record every deviation.
3. Thirty safe real requests: the conductor works inside this contract and a
   person joins only on escalation.
4. Full safe-request mode only after the recorded quality criteria pass.

Pause the route immediately on wrong-peer risk, stale estimate, duplicate
order, unapproved financial side effect or privacy breach.  Return to manual
handling until the defect is fixed and independently verified.
