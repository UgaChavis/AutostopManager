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
8. If the file changes durable knowledge routes, update route-card metadata in
   `knowledge_map.json`: `aliases`, `keywords`, `questions`, and
   `source_of_truth_files`.
9. Run `sync_knowledge_base`, then `audit_knowledge_base`.
10. If the file is a gearbox or transmission corpus, route its durable rules
   into `docs/agent/transmission_playbook.md` and refresh transmission source
   routing in `docs/agent/automotive_sources/data_type_source_map.json`.

## File Classes

- `docs/agent/knowledge_base_index.md` is the human entrypoint for durable
  knowledge navigation.
- `docs/agent/knowledge_map.json` is the machine-readable navigation map and
  route-card source for cheap `probe_knowledge_base` checks.
- `docs/agent/*.md` and `docs/agent/*.json` are for durable operating rules.
- `docs/agent/automotive_sources/*` is for curated automotive source routing
  and ingestion guidance.
- `data/` is for temporary evidence, audit output, or verification artifacts.
- `downloads/` or workspace temp files are disposable unless promoted into a
  playbook or catalog.

## Placement Rules

- Put broad behavior in `autostop_manager_skill.md`, `manager_rules.json`, or
  `operating_playbook.json`.
- Put task workflows in a specific `*_playbook.md`.
- Put source lists and routing data in JSON catalogs.
- Put vehicle/model-specific deep knowledge into a dedicated Codex skill under
  the local Codex skills directory when it should auto-trigger in future chats.
- Put raw source files only in a clearly named `source-cache/` or workspace
  evidence folder when the license allows local storage.
- Add every new durable route to `knowledge_base_index.md` and
  `knowledge_map.json`.
- For every route, include aliases in Russian and English, common owner
  phrasing, model codes, likely typos, key systems, common tasks, and the first
  source-of-truth file to open.

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
- which index/map entries were updated, if any

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

Use `probe_knowledge_base` before broad reads. If it returns
`has_knowledge=true`, open `source_of_truth` first; if it returns false, route
to external/OEM sources and consider adding a new route after the work.

For gearbox/transmission corpora, keep only reusable operating rules, symptom
maps, fluid constraints, source hierarchy, and safety notes. Do not store raw
manual copies or copied service databases.

For model-specific corpora such as BMW X5 F15/N63TU, keep a small trigger
skill plus focused reference files. The main project index should link to the
skill, but should not duplicate the full model notes.
