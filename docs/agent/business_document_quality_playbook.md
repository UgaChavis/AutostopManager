# Business Document Quality Playbook

Purpose: route AutoStop PDF, Word, Excel, счета, акты, КП, receipts, requisites
sheets, accounting-style files, and printable templates through one strict
quality gate before delivery, email, or CRM upload.

## Open First

Use this playbook as the active route. Then use only the available file-format
tooling needed by the artifact. If a named local skill or connector is not
installed in the current environment, use bundled workspace/runtime tooling and
state which render or audit gate was available:

- XLSX/Excel/CSV: spreadsheet library, formula/value check, and print-area
  inspection.
- PDF/print form: render every page, inspect visually, and extract text only as
  supporting evidence.
- DOCX/Word: generate/export, then render or inspect the final pages before
  delivery.

## Required Behavior

1. Determine whether the task is a final external document, draft, template, or
   internal CRM file.
2. Use `business_identity` for current AutoStop/IP реквизиты when company facts
   are needed. Do not copy private bank/contact facts into Git-tracked docs.
3. For invoices, acts, КП, and accounting-like documents, verify:
   - реквизиты and counterparty facts;
   - document number and date;
   - services/items, quantities, prices, discounts, totals;
   - НДС wording/status, including "в том числе НДС" when required;
   - signature/stamp blocks and logo/header/footer placement;
   - page breaks, print area, and visual alignment.
4. Render and inspect the final artifact before delivery:
   - every PDF/DOCX page;
   - every meaningful XLSX sheet or print area.
5. If the document is a regulated/tax form such as счет-фактура or УПД, verify
   current requirements from official/current sources before finalizing.

## PDF Invoice Gate

For generated invoices:

- calculate line totals, markup, discounts, tax, and final total from source
  rows, then cross-check the rendered PDF text;
- verify buyer/seller names and реквизиты against the source email/file/CRM
  facts for this task;
- show НДС wording explicitly when the owner asks for it;
- render pages locally and inspect that logos, totals, signatures, and stamps
  are visible and not clipped;
- keep generated files such as `generated_invoices/` out of Git unless the
  owner explicitly promotes them as fixtures or templates.

## Output Discipline

In the final answer, name the artifact and the verification performed. If a
gate could not run, say which gate was skipped. Do not claim a document is
print-ready without render evidence.
