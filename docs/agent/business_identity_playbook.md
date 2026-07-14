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
  inventory of the owner's synced document folder with dates, hashes, and topic
  flags. This runtime file is optional in Git checkouts and should be created
  locally when business-document routing is needed.

These files are under `data/`, which is ignored by Git. They may be absent in a
clean checkout, and that must not break `knowledge-audit` or tests. When absent,
route through this playbook and the `business_identity` annotation, but state
that exact current реквизиты are unavailable until the local runtime files are
restored. Do not move them into tracked docs and do not paste their private
banking/contact contents into public playbooks.

## Current Selection Rule

Use `data/private_knowledge/business_identity_current.json` when present.
Otherwise inspect `data/private_knowledge/business_documents_inventory.json`,
select the newest reliable document for
the requested fact class, and verify the original before external use. A file
name or old modification date in a tracked playbook is never proof of current
requisites. Historical tender, registration, offer, and lease documents are
sources only when the owner explicitly asks for history.

## Use When

- the owner asks for ИП реквизиты, карточку предприятия, INN/OGRNIP/OKVED,
  bank details, address, current AutoStop contact details, or commercial-offer
  boilerplate;
- a CRM card, quote, invoice, tender response, or document draft needs current
  business identity facts;
- the owner asks what is old versus current in the local document inventory.

## Safety Rules

- Treat CRM and accounting systems as source of truth for live financial state.
- Treat this route as a private local knowledge shortcut, not a public document.
- Before external use, re-check the original source file if exact bank branch
  wording, signature block, or legal formatting matters.
- Do not store supplier passwords, bank-client credentials, scans of IDs, or
  full contracts in memory.
