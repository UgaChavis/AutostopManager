# Offline Parts Catalogs Pack

Canonical tracked registry:
`sources/offline_parts_catalog_sources.json`. Optional extracted runtime data
lives only under ignored `data/offline_parts_catalogs/`.

Use the cache after VIN/EPC or an exact vehicle profile is established. Search
`data/offline_parts_catalogs/text` by exact OEM/article/engine code and retain
publisher, URL, version, checked number and confidence only in internal
evidence. Never promote aftermarket crosses to OEM, paste catalog tables into
CRM/memory, or use cracked EPC dumps. MAN OEM numbers require authorized
webMANTIS/PartsBase/partslink24, dealer or supplier evidence.

To rebuild, download an official registry URL, verify its recorded hash,
extract searchable text/CSV into the ignored runtime tree, update
`catalog_index.json`, then run `knowledge-sync`.
