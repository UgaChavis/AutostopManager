# Business Documents

Use the CRM print module and standard AutoStop templates for service documents,
not parallel PDF/HTML forms. With a card use
`agent_document_workflow(operation="download_repair_order_print_pdf")`; without
one use `create_document_without_card_pdf`. Let the workflow infer an explicit
standard type and preserve its `tax_label`.

Manual completion acts use `save_completion_act_form`; reset only through the
separately authorized `reset_completion_act_form`. Retain the verified snapshot
for recovery, and treat `reset_tombstone` as history rather than an active form.

Current requisites come only from ignored runtime data, preferably
`data/private_knowledge/business_identity_current.json`, with
`data/private_knowledge/business_documents_inventory.json` as evidence fallback.
Verify the original;
old contracts and tenders do not establish current details. If evidence is
missing, say so. Never commit identity, banking, contact or credential data.

Before external delivery, check the facts and calculations relevant to the
document and visually inspect every meaningful rendered page or sheet. For a
regulated form, verify current official requirements. Parsed text is not render
evidence. Keep private output outside Git and report only checks actually made.
