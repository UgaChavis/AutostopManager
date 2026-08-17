# CRM Card Text Standard

Canonical standard for AutoStop CRM card `description` and `board_summary`.
All card-creation, cleanup, VIN/OEM, service-management and director workflows
link here instead of defining their own shape, length or wording rules.

## Meaning Of The Fields

`board_summary` is the manager's short, current explanation of what is happening
with the vehicle now, what it is waiting for, or what should happen next. Write
one or two natural sentences in ordinary clean text. It should sound like a
colleague briefly explaining the situation, not a status code, a rigid template
or a telegraphic list. Do not repeat the vehicle make unless it prevents
ambiguity. Do not use Markdown or decorative emoji.

`description` is the complete, coherent and gradually developing story of the
vehicle: why it arrived, what was found, what was agreed, what was done, what is
happening now and what remains to do. Its purpose is to let another employee
understand and continue the case without reconstructing it from the event log.
It is not a fixed form, a mandatory chronology or an internal research report.

## Editing The Story

Whenever a new confirmed fact appears, reread the entire existing description
and edit it as one text. Weave the fact into the most natural place, correct
outdated wording, retain useful history, remove repetition and improve the flow.
Do not merely append the latest event and do not reduce a meaningful history to
a few words.

Choose the composition, length, paragraphs and wording for the actual card.
There are no mandatory headings, blocks, dates, line counts or fixed order of
facts. A small case may be brief; a complex or long-running repair may need a
substantial history. Operational completeness and readability decide the
length.

The agent is also the editor: split dense text into useful paragraphs, fix
spacing, move fragments when that improves comprehension and remove obsolete
duplication. CRM-supported formatting may be used moderately in `description`:
`**bold**` for genuinely important emphasis, `*italic*` for a rare soft remark
and `++underline++` for a key value. Emoji are rare and only for faster reading.
Formatting is optional and must not turn the story into a mechanical form.

## Facts To Preserve And Exclude

Preserve every confirmed fact that remains useful for the work, including the
complaint, findings and diagnostic results, agreed scope, completed work,
current work or wait, parts state, customer arrangement and next action.
Technical information such as fluid volumes and approvals, part numbers,
measurements and diagnostic results belongs in `description` when confirmed and
useful for later work.

Do not add invented events, guesses, the agent's internal reasoning, search
sources or methods, confidence labels, price/evidence matrices, supplier-check
boilerplate, private contacts, raw VIN, long correspondence, raw scans or other
service noise. Evidence, provenance, confidence and price matrices stay in the
internal lookup result, owner report or protected workflow record. Put phone,
plate, mileage and vehicle identity in structured CRM fields whenever possible;
repeat them in public text only when the owner explicitly requests it or the
work genuinely requires the value to remain visible.

Use only CRM-supported Markdown in `description`; never use raw HTML or visible
pseudo-formatting.

## Keeping Current State Aligned

When the current state changes, update `description` and `board_summary`
together in the same protected write. The full story must reflect the new state
without contradicting its history; the summary is a fresh natural-language
condensation of what matters now, not an independent version of the case.

In director mode the agent applies this standard autonomously after receiving a
new confirmed fact: it decides how to revise the whole story and how to express
the current state briefly for the board. A clear card is not rewritten merely
to demonstrate activity.

## Write Flow

Every write keeps the existing safety sequence:

`exact card reread -> prepare_action_contract -> cleanup_card dry-run -> apply
with a new idempotency key -> independent exact reread and verification`.

The final reread verifies the complete `description`, exact `board_summary`,
`board_summary_stale=false`, and that no unplanned card fields changed.
