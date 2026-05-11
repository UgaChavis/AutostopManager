# Business Identity Private Knowledge Route

Purpose: route owner-authorized questions about current ИП / AutoStop business
requisites, company card data, commercial-offer identity, and historical
Гришкявичус tender templates without copying private business details into
Git-tracked docs.

## Source Of Truth

Use this playbook as the public route and load local private runtime files only
when they exist:

- `data/private_knowledge/business_identity_current.json` - current curated
  business identity facts selected from the newest reliable documents.
- `data/private_knowledge/business_documents_inventory.json` - full filesystem
  inventory of `C:/Users/User/Мой диск/ДОКУМЕНТЫ` with dates, hashes, and topic
  flags.

These files are under `data/`, which is ignored by Git. They may be absent in a
clean checkout, and that must not break `knowledge-audit` or tests. When absent,
route through this playbook and the `business_identity` annotation, but state
that exact current реквизиты are unavailable until the local runtime files are
restored. Do not move them into tracked docs and do not paste their private
banking/contact contents into public playbooks.

## Current Selection Rule

Use only the newest reliable source for current facts:

1. Current legal and bank requisites: `Карточка предприятия (1).doc`,
   last modified `2025-10-28T15:52:24+07:00`.
2. Current customer-facing commercial/service wording: `КП НОВОЕ!.doc`,
   last modified `2025-10-22T13:28:01+07:00`.
3. Latest historical tender response wording for ИП Гришкявичус:
   `Тендер Администрация/Ответ на запрос ИП Гришкявичус.docx`, last modified
   `2020-11-09T15:43:47+07:00`.
4. Historical fallback registration source: `выписка.pdf`, generated in 2019.

Older response drafts, old commercial offers, and scanned lease files are not
current sources unless the owner explicitly asks to inspect history.

## Use When

- the owner asks for ИП реквизиты, карточку предприятия, INN/OGRNIP/OKVED,
  bank details, address, current AutoStop contact details, or commercial-offer
  boilerplate;
- a CRM card, quote, invoice, tender response, or document draft needs current
  business identity facts;
- the owner asks what is old versus current in `C:/Users/User/Мой диск/ДОКУМЕНТЫ`.

## Safety Rules

- Treat CRM and accounting systems as source of truth for live financial state.
- Treat this route as a private local knowledge shortcut, not a public document.
- Before external use, re-check the original source file if exact bank branch
  wording, signature block, or legal formatting matters.
- Do not store supplier passwords, bank-client credentials, scans of IDs, or
  full contracts in memory.
