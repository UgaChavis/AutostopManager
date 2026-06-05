# Offline Parts Catalogs Knowledge Pack

Purpose: keep a durable route to local offline parts-catalog evidence without
committing large PDF/XLSX catalogs or copied catalog tables to Git.

## Local Runtime Cache

The runtime cache lives here when present:

- `data/offline_parts_catalogs/catalog_index.json`
- `data/offline_parts_catalogs/pdf/`
- `data/offline_parts_catalogs/xlsx/`
- `data/offline_parts_catalogs/text/`
- `data/offline_parts_catalogs/csv/`

`data/` is intentionally ignored by Git. A clean checkout may not have these
files; in that case, use `sources/offline_parts_catalog_sources.json` to
re-download official public sources as needed.

## Load Order

1. Open `docs/agent/vin_oem_lookup_playbook.md` for OEM-vs-cross boundaries.
2. Check `data/offline_parts_catalogs/catalog_index.json` if it exists.
3. Search extracted text with a narrow number or vehicle query:

```bash
rg -n "<OEM-or-article-or-engine-code>" data/offline_parts_catalogs/text
```

4. Open the matching source entry from
   `sources/offline_parts_catalog_sources.json` to identify title, publisher,
   source URL, scope, and limits.
5. If needed, inspect the original PDF locally by path from
   `catalog_index.json`.

## Current Cache Contents

As of 2026-06-05, the local cache was built from official public sources for:

- MAHLE/Knecht filter catalog 2026 and filter interchange XLSX.
- MAHLE heavy-duty commercial vehicle engine-parts catalog AP-37-16.
- Bosch filter catalogue 2023/2024 and commercial vehicle filter brochure.
- MANN-FILTER trucks/buses 2024-2026 catalog and cross-reference list.
- NGK/Niterra UK spark plug and diesel glow plug catalogue 2024.
- Donaldson Engine Liquid Filtration Product Guide.
- ZF Aftermarket/SACHS Asian commercial vehicle clutch leaflet.

MAN has no public legal offline EPC cache in this pack. For MAN OEM numbers,
use authorized MAN Service Portal/webMANTIS, MAN PartsBase/partslink24, dealer,
or supplier confirmation first. Official MAN PDFs found during intake describe
PartsBase/webMANTIS and genuine parts availability, but they are not a
VIN-specific offline parts catalog.

## Safety Limits

- These files are aftermarket, supplier, brochure, cross-reference, or product
  guide evidence unless a source explicitly says otherwise.
- Do not promote a MAHLE/Bosch/MANN/Donaldson/NGK/ZF article to an OEM result.
- For VIN-sensitive work, OEM candidates must come from VIN/frame-specific EPC,
  brand portal, dealer, or a configured catalog provider.
- Use offline catalogs to check categories, dimensions, engine/date notes,
  OE-reference lines, competitor crosses, kit contents, and footnotes.
- Keep CRM notes compact: source title, publisher, version/date, checked number,
  and confidence. Do not paste catalog tables into CRM or durable memory.
