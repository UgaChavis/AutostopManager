# PartsAPI Method Contract

`partsapi_catalog_lookup` is the canonical adapter. The live registry belongs
to `PARTSAPI_OPERATIONS` and `PARTSAPI_METHOD_KEY_ENV_NAMES` in
`autostop_manager/catalog_clients.py`; do not copy its method table or key list
into documentation.

## Configuration

- Set `PARTSAPI_BASE_URL` and either the global key or the exact method key.
- An exact method key replaces the global key; a missing global key alone is
  not a configuration failure.
- Never persist keys, raw VIN/frame/plate values, or provider payloads in docs,
  tests, CRM, Git, durable memory, or broad logs.

## Semantic Boundaries

- Provider/auth/config errors and empty results are `inconclusive`, not proof
  that a part is absent, applicable, or inapplicable.
- A VIN derived from a registration plate is only an identity lead. Verify it
  against the physical vehicle and exact CRM context before any write.
- `getPartsbyVIN` requires a numeric `cat`. Text intent is only a routing hint;
  return `category_unresolved` instead of inventing an identifier.
- Empty applicability or cross payloads remain empty/inconclusive.
- Cross, article, and localized-name data are aftermarket enrichment, not OEM
  or VIN-fitment proof; keep `fitment_confirmed=false`.

## Labor And Fluids

- AUTONORMS TopCatId/SubCatId is a separate namespace from the PartsAPI product
  group index used by `getPartsbyVIN`.
- `workTime` is duration evidence, not hourly rate or final price; avoid
  double-counting shared remove/install work. `workPrice` is not AutoStop's
  selling price.
- Fill-volume rows are quantity evidence only. They do not confirm a fluid
  approval, product selection, or final material price.

## Normalized Output

- `vehicle_profiles`: redacted identity and modification summaries.
- `oem_candidates`: VIN/frame-specific original candidates.
- `cross_candidates`: replacement candidates, never automatic fitment proof.
- `article_candidates`: TecDoc article/name/criteria enrichment.
- `empty_payload`: a valid empty provider response, still inconclusive.

## Operational Limits

- Keep retries and live calls bounded, cap broad result sets, and expose only
  redacted request/result metadata.
- A smoke run proves adapter behavior only; it does not prove fitment and never
  authorizes CRM writeback.
- Final identity, applicability, labor-price, fluid, and CRM decisions remain
  with their dedicated workflows and write gates.
