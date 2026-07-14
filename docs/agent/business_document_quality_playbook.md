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
2. AutoStop service documents must go through the CRM print module and standard
   AutoStop templates. This includes счета, акты, заказ-наряды, акты приема,
   счет-фактуры, дефектовки, акты выполненных работ, and продажа запчастей.
   For a CRM card use
   `agent_document_workflow(operation="download_repair_order_print_pdf")`; for
   "Документ без карточки" use
   `agent_document_workflow(operation="create_document_without_card_pdf")`.
   Do not build independent PDF/HTML templates for these AutoStop documents.
3. Use `business_identity` for current AutoStop/IP реквизиты when company facts
   are needed. Do not copy private bank/contact facts into Git-tracked docs.
4. For invoices, acts, КП, and accounting-like documents, verify:
   - реквизиты and counterparty facts;
   - document number and date;
   - services/items, quantities, prices, discounts, totals;
   - НДС wording/status, including "в том числе НДС" when required;
   - signature/stamp blocks and logo/header/footer placement;
   - page breaks, print area, and visual alignment.
5. Render and inspect the final artifact before delivery:
   - every PDF/DOCX page;
   - every meaningful XLSX sheet or print area.
6. If the document is a regulated/tax form such as счет-фактура or УПД, verify
   current requirements from official/current sources before finalizing.

## AutoStop CRM Print Route

- Documents with an existing CRM card: call
  `agent_document_workflow(operation="download_repair_order_print_pdf")` and
  select the needed standard AutoStop template.
- Documents without CRM cards: call
  `agent_document_workflow(operation="create_document_without_card_pdf")` or
  open the CRM print module in "Документ без карточки" mode, then provide the client,
  реквизиты, vehicle, works, materials, payments, dates, numbers, `tax_label`
  (`НДС (5%)` or `Без НДС`), and comments.
  If the request text already says `акт выполненных работ`, `дефектовка`,
  `заказ-наряд`, `счет-фактура`, or `продажа запчастей`, the CRM MCP client can
  infer the standard document type; pass `document_type` only when the text is
  ambiguous or the owner explicitly names a different type.
- The CRM print module owns PrintServiceProfile, template rendering, PDF render,
  preview, export, and print behavior. The manager agent must not replace it
  with a separate PDF/HTML generator for AutoStop documents.
- Approved practical flow for ready CRM cars, including repeated "ВашАвто /
  Ваш Авто" requests: identify the live ready cards/repair orders in CRM, export
  the requested standard documents through the CRM print module, save runtime
  artifacts under `out/<document-family>-<client-or-scope>-<date>-final/`, render
  page previews with `pdftoppm`, extract text with `pdftotext`, and deliver the
  final PDF links in chat only after page count, non-empty pages, line items,
  totals, and signature/warranty or invoice wording have been checked.
- For `заказ-наряд` exports, the accepted template behavior is: works/materials
  tables may continue across pages row-by-row, totals must remain visible, and
  the warranty/important-terms block may remain on its own forced page when the
  standard CRM template inserts that break.

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
