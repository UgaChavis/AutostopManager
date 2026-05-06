# Business Document Quality Playbook

Purpose: route AutoStop PDF, Word, Excel, счета, акты, КП, receipts, requisites
sheets, accounting-style files, and printable templates through a stricter
quality gate before delivery or CRM upload.

## Open First

Use this playbook as the active route. Then load only the file-format skill or
tooling needed by the artifact:

- XLSX/Excel/CSV register: `spreadsheet`
- PDF/print form/extraction/source review: `pdf`
- DOCX/Word: use the available local document tooling, then render or export
  for visual inspection before delivery.

## Required Behavior

1. Determine whether the task is a final external document, a draft, a template,
   or an internal CRM file.
2. Use `business_identity` for current AutoStop/IP реквизиты when company facts
   are needed. Do not copy private bank/contact facts into Git-tracked docs.
3. For invoices, acts, КП, and accounting-like documents, verify:
   - реквизиты and counterparty facts;
   - document number/date;
   - services/items, quantities, prices, discounts, totals;
   - НДС wording/status;
   - signature/stamp blocks;
   - page breaks and print layout.
4. Render and inspect the final artifact before delivery:
   - every DOCX/PDF page;
   - every meaningful XLSX sheet or print area.
5. Run the strongest available quality gate for the artifact: formula/value
   checks for spreadsheets, PDF render inspection for printable forms, and a
   visual page pass for DOCX/PDF. If a dedicated audit script is installed in a
   future environment, run it as an additional gate.
6. If the document is a regulated/tax form such as счет-фактура or УПД, verify
   current requirements from official/current sources before finalizing.

## Output Discipline

In the final answer, name the artifact and the verification performed. If a
gate could not run, say which gate was skipped. Do not claim a document is
print-ready without render evidence.
