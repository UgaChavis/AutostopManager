# Store quote conductor

`store_quote_conductor` leads one Store request to a useful choice and, only
after clear consent, one unpaid order. AutoStop App owns the request, Admin V2
`estimate_draft`, publication, cabinet and SalesOrder; Telegram owns the dialog.
Manager keeps only technical refs, versions, hashes and outcomes.

## Intake and dialogue

Before sourcing or contacting the customer, read the exact Admin V2 request as
`full_with_vin_photo`; inspect the protected preview when it has a VIN photo.
Use the request's existing lines, comments, vehicle data, delivery details and
phone. The phone resolves one private `work` peer; that is enough to bind the
client. Never send an authorship check such as “Вы оставляли заявку?”. If it
does not resolve uniquely to a private peer, do not use Telegram.

Use the sources that materially reduce uncertainty: Store stock/sourcing,
contracted suppliers, local retail, Drom/Avito or the web. Marketplace data may
support a range but not fitment or availability. Keep preliminary dialogue
short, natural and useful; ask only a missing fact that changes fitment or the
choice. It may orient the client but cannot publish, reserve or create an order.

Give a small, understandable set of options and a practical recommendation.
The number and mix are situational. Do not present tentative fitment, stock,
lead time or price as confirmed, or expose procurement prices, internal sources
or automation details.

## Publication and order

The canonical customer quote is Admin V2 `estimate_draft`; legacy, manual or
mixed QuoteOffer cases go to a person. Each original line needs an offer or a
customer withdrawal. Final options need fitment, article, availability, lead
time, client price and Store warranty evidence.

Before any customer-visible Store change: exact reread → action contract →
dry-run → idempotent apply → independent readback. Publication precedes final
delivery. A failed delivery leaves the published revision intact; stale,
ambiguous or uncertain replies never create an order.

Only clear consent tied to the current published quote creates one
`WAITING_FOR_PAYMENT` order. The workflow never reserves stock, procures,
discounts, records payment, touches a cashbox or sends card details. Those
requests, a wrong/uncertain peer, incomplete evidence or a conflict go to a
person. Improve from de-identified reviewed cases and replace obsolete guidance
rather than adding one-off rules.
