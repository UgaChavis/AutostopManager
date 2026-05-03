# Knowledge Intake Playbook

Purpose: accept new files, datasets, notes, and references without turning
the project into a second raw-data store.

## When To Use

Use this playbook when the owner provides:

- a new markdown or text document
- JSON, JSONL, CSV, or spreadsheet files
- PDF manuals, scans, or excerpts
- source catalogs or routing tables
- bookmarks or links to authoritative sources

## Intake Rule

Do not copy whole files into manager memory.

Store only:

- durable conclusions
- routing rules
- source preferences
- compatibility caveats
- short follow-up tasks

Keep raw files in the workspace for inspection, but do not treat them as
memory records unless the owner explicitly asks for archiving.

## Recommended Workflow

1. Identify the file type, source, and purpose.
2. Check whether the file is open/public, owner-supplied, or license-bound.
3. Decide whether the file is a source of truth, a reference, or a temporary
   working artifact.
4. Extract only the durable rules or facts that should survive future sessions.
5. Update the relevant playbook, source catalog, or memory rule file.
6. Record the change in `manager_journal`.
7. If the file changes the MCP surface, refresh the corresponding catalog.

## File Classes

- `docs/agent/*.md` and `docs/agent/*.json` are for durable operating rules.
- `docs/agent/automotive_sources/*` is for curated automotive source routing
  and ingestion guidance.
- `data/` is for temporary evidence, audit output, or verification artifacts.
- `downloads/` or workspace temp files are disposable unless promoted into a
  playbook or catalog.

## Source / License Check

Before persisting conclusions, determine whether the source is:

- open/public
- owner-provided
- licensed commercial data
- restricted or unsafe to copy

If the source is licensed or restricted, store only the route, not the copied
content.

## Output Contract

When a new file is handled, produce:

- file name or path
- classification
- what changed in memory or docs
- what remains for later verification
- whether the source is safe to reuse

## Memory Rule

New files can update memory only if they add a reusable operating rule or a
durable owner preference.

Do not store:

- full tables
- full manuals
- raw OCR dumps
- copied repair databases
- entire spreadsheets
- temporary search results

## Tooling

Use this sequence when the file is automotive-related:

1. `docs/agent/vehicle_identity_playbook.md`
2. `docs/agent/vin_oem_lookup_playbook.md`
3. `docs/agent/automotive_repair_source_playbook.md`
4. `docs/agent/fluid_maintenance_playbook.md`
5. `docs/agent/parts_search_playbook.md`
6. `docs/agent/zzap_search_playbook.md`

If the file changes the supported MCP tools or memory flow, update the local
catalogs first, then remember the new durable rule.
