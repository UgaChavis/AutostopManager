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

## Client result and escalation

Do not stop at an internal report. Search broadly with the sources that can
answer this request: Store stock and `store_sourcing_offer`, ROSSKO and
contracted suppliers, Krasnoyarsk public retail, Drom/Avito and the web. Choose
the order by urgency and evidence quality; do not stop after one weak source.
Marketplace text can widen a market range; it never proves fitment.

Keep a clear difference between two kinds of client answer:

- **Preliminary orientation** is a useful short Telegram answer with candidate
  article(s), a market corridor and the next check. It honestly separates what
  is likely from what still needs verification. It is not an Admin V2 estimate
  and cannot draft, publish, reserve or create an order.
- **Confirmed estimate** has exact application, article, availability, lead
  time, client price and Store warranty. Show up to three clear choices: an
  original if available and up to two verified analogues.

Conflicting catalogues, incomplete evidence or unavailable stock stop only the
confirmed-estimate path, not the conversation: give the useful preliminary
result first, then hand off if a person must obtain the missing proof. A
discount, supplier purchase, reservation, unusual delivery, payment confirmation
or manual/mixed Store estimate is a direct handoff. The Store-owned warranty
text must exist before publication; Manager never invents it.

## Telegram dialog

Use the `work` account and one exact current private peer. Start the business
conversation from the request only after the current Store–Telegram binding is
confirmed. Zero, multiple, non-private, conflicting or unconfirmed routes stay
a neutral identity check with no request detail. Identity is a transport
prerequisite, not a script for the customer: the bridge should prove an
unambiguous natural reply by meaning rather than demand a magic answer format.
Until it can, Manager fails closed instead of inventing confirmation. Never make
the customer repeat a photo, VIN or comment already attached to the request.

Each sent message is transient and tied to the same request. Speak briefly and
naturally: notice what the client has already sent, move the conversation
forward, and ask what is useful now. For example: “Фото увидел. Нужен полный
комплект? Оригинал смотреть или хороший аналог тоже подойдёт?” This is a pattern,
not a script. Do not disclose purchase prices, internal sources, automation
details, payment requisites or a tentative fact as confirmed.

Only an estimate published in Store is bound to its revision and context hash.
The typed adapter must independently prove the private peer and delivery before
it treats an inbound reply as a selection or consent. Identity confirmation and
a preliminary orientation never trigger an order.

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

After an owner-observed test, update only the canonical rule or regression test
that explains the result, and delete the superseded duplicate. Never preserve a
chat, VIN, offer or a one-off workaround as a permanent instruction.

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
