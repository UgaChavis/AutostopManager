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
- `sources/citations.md` - browsed source citations and evidence notes.
- `rules/offline_parts_catalog_usage_rules.jsonl` - compact machine-readable
  usage rules for offline catalog checks.

## Runtime Files Generated On 2026-06-05

The generated runtime cache is intentionally untracked:

- `data/offline_parts_catalogs/catalog_index.json`
- `data/offline_parts_catalogs/README.md`
- `data/offline_parts_catalogs/pdf/*.pdf`
- `data/offline_parts_catalogs/xlsx/*.xlsx`
- `data/offline_parts_catalogs/text/*.txt`
- `data/offline_parts_catalogs/csv/*.csv`

The generated cache contained 10 source files, about 197 MB total, with PDF
text extracted by `pdftotext -layout` and MAHLE XLSX rows extracted to text/CSV.

## Rebuild Outline

1. Download each source URL listed in
   `sources/offline_parts_catalog_sources.json`.
2. Store PDFs under `data/offline_parts_catalogs/pdf/` and spreadsheets under
   `data/offline_parts_catalogs/xlsx/`.
3. Extract text to `data/offline_parts_catalogs/text/<catalog_id>.txt`.
4. Record SHA-256, bytes, page count, source URL, retrieved date, scope, and
   limits in `data/offline_parts_catalogs/catalog_index.json`.
5. Run `python -m autostop_manager.cli knowledge-sync` so optional runtime
   metadata is indexed when present.
