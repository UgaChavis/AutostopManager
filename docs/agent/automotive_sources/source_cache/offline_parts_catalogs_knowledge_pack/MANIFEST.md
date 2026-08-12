# Offline Parts Catalogs Pack Manifest

Pack path:
`docs/agent/automotive_sources/source_cache/offline_parts_catalogs_knowledge_pack/`

Runtime cache path:
`data/offline_parts_catalogs/`

## Tracked Files

- `README.md` - purpose, load order, local cache path, and safety limits.
- `MANIFEST.md` - this file.
- `sources/offline_parts_catalog_sources.json` - official source records and
  local cache filenames.
- `rules/offline_parts_catalog_usage_rules.jsonl` - compact machine-readable
  usage rules for offline catalog checks.

## Runtime Files Generated On 2026-06-05

The generated runtime cache is intentionally untracked:

- `data/offline_parts_catalogs/catalog_index.json`
- `data/offline_parts_catalogs/README.md`
- `data/offline_parts_catalogs/text/*.txt`
- `data/offline_parts_catalogs/csv/*.csv`

The compact cache retains searchable text/CSV, source URLs and hashes. Ten
verified PDF/XLSX originals (148 MB) were removed after extraction; restore an
original only when page layout or source context is required.

## Rebuild Outline

1. Download the required source URL listed in
   `sources/offline_parts_catalog_sources.json`.
2. Store PDFs under `data/offline_parts_catalogs/pdf/` and spreadsheets under
   `data/offline_parts_catalogs/xlsx/`.
3. Extract text to `data/offline_parts_catalogs/text/<catalog_id>.txt`.
4. Record SHA-256, bytes, page count, source URL, retrieved date, scope, and
   limits in `data/offline_parts_catalogs/catalog_index.json`.
5. Run `python -m autostop_manager.cli knowledge-sync` so optional runtime
   metadata is indexed when present.
