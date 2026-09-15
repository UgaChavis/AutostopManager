# Operations reference

Use only the relevant section. Shared boundaries live in [AGENTS.md](../../AGENTS.md).

## CRM cleanup

For a requested cleanup, use `agent_board_workflow(operation="cleanup_card")` with
the current card, action contract, revision and idempotency. Patch only unclear
facts and next actions; preserve history, manual diagnosis and unrelated fields.
Resolve client/vehicle links through their dedicated actions. Moving, archiving,
deadlines and finance need their own authorization. Reread the changed card.

## Documents and Gmail

Use CRM printing and AutoStop templates for CRM documents, not parallel PDF/HTML.
Manual acts use `save_completion_act_form`; resetting needs separate permission
and a recovery snapshot. Confirm current requisites and applicable official form
requirements; old documents are not proof of current details. Check calculations
and rendered output before delivery, not just extracted text.

Use the active Gmail connector for the requested scope; inspect attachment metadata
before downloading. Confirm the recipient and artifact before sending. For requested
mail cleanup, archive to retain mail; Trash only when deletion was requested.
