# Store quote conductor

`store_quote_conductor` leads one Store request from clarification to a single
unpaid order. Its job is to give the customer a useful answer quickly while
keeping confirmed terms and tentative guidance clearly separate.

## Ownership and durable state

AutoStop App owns the request, Admin V2 `estimate_draft`, publication, cabinet
and SalesOrder. Telegram owns the dialog. Manager keeps only technical IDs,
versions, hashes, workflow state and verification outcomes; it does not retain
chat text, contacts, VINs, photos, prices, supplier evidence or payment data.

The canonical client quote is the Admin V2 estimate. QuoteOffer records are
legacy evidence only: a manual, mixed or conflicting request goes to a person
and is never automatically rewritten. One active, versioned workflow owns each
request and can resume after a wait or uncertain transport result.

An estimate may cover an original request line or a `conversation_added` line
supported by the current confirmed dialog. In both cases the final option needs
the same evidence: fitment, article, availability, lead time, client price and
the Store-owned warranty terms. Every original line must have an offer or be
explicitly withdrawn by the customer.

## Work with the customer

Search widely enough to answer the actual request: Store stock and sourcing,
ROSKKO or contracted suppliers, suitable Krasnoyarsk sellers, Drom/Avito and
the web. Choose sources and options by usefulness, urgency and evidence quality.
Marketplace information can shape an honest corridor; it cannot confirm
fitment by itself.

Do not leave the customer waiting for perfect certainty. Once the exact private
`work` peer is bound to the Store request, ordinary transient Telegram may give
a short preliminary orientation: likely articles or options, an approximate
range, what is still being checked, and one useful question. This is not a
Store estimate and cannot save, publish, reserve or create an order.

Speak like a local service manager: brief, natural and attentive to what the
person already said. Ask only what moves the choice forward. Do not make them
repeat a VIN, photo or comment already present, and do not expose procurement
prices, internal sources, automation details or tentative facts as confirmed.

When the evidence is complete, present a small, understandable choice and a
practical recommendation. The number and mix of options should fit the case;
quality, delivery time and sensible price matter more than a fixed template.

## Publish and order

Before a customer-visible Store change, reread the exact request and revision,
prepare the action contract, dry-run, apply with a fresh idempotency key, then
independently reread. Missing warranty text, incomplete coverage, revision
conflict or uncertain outcome blocks final publication, not a useful preliminary
reply.

Store publication comes before the final Telegram message. The typed adapter
must prove the same private peer, published revision, snapshot/context binding
and delivery readback. A failed send leaves the Store publication intact and
resumes without republishing.

Only clear consent tied to that current delivered quote may create an order.
Questions, ambiguity and stale replies continue the dialog. Telegram and the
customer cabinet converge on the same Store order in `WAITING_FOR_PAYMENT`, so
concurrent confirmation returns the existing order rather than a duplicate.

The workflow never reserves stock, purchases from a supplier, grants a
discount, records payment, touches a cashbox or sends card details. It may say
that payment is available at reception or according to an employee's
instructions. Requests for those financial actions, unusual supply terms, or
manual/mixed estimates are handed to a person.

## Recovery and improvement

Wrong or uncertain peer, stale publication, duplicate-order risk, privacy risk
or an unapproved side effect stops automation and preserves evidence for a
human decision. A missing deployed typed sender or identity-binding route is
also a handoff, never a reason to forge confirmation. A lost response stays
unresolved until exact readback.

Improve this route from de-identified outcomes and owner-reviewed regression
cases. Promote through synthetic and supervised examples before broader use;
replace obsolete guidance instead of accumulating one-off rules.
