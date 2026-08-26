# Vehicle Identity Playbook

Compact guardrail for identifying a vehicle before parts, recall, or
compatibility work. Runtime behavior belongs to the code below; this document
does not duplicate market tables, provider schemas, or source lists.

## Canonical Owners

- Identifier classification, normalization, and redaction:
  `autostop_manager/vin_lookup.py`.
- WMI/frame hints, CRM reconciliation, confidence, conflicts, and batch decode:
  `autostop_manager/vehicle_identity.py`.
- Source metadata and brand routing: `autostop_manager/vin_sources.py` and
  `docs/agent/vin_oem_sources.json`.
- Provider readiness: `autostop_manager/catalog_adapters.py`.
- VIN/frame-specific OEM resolution: `autostop_manager/vin_oem_resolver.py`.
- Parts sourcing after identity: `docs/agent/parts_search_playbook.md`.

## Guardrails

- Classify the input as ISO VIN, partial VIN, frame/body number, market code,
  plate-derived lead, or unknown before decoding it.
- Never invent or pad a 17-character VIN. JDM frame numbers remain frame
  numbers and may need both compact and hyphenated query forms.
- Keep raw VIN, frame, plate, and document contents transient. Responses,
  durable memory, docs, and broad logs use only redacted identity.
- vPIC is an official baseline decoder, not an EPC or parts-fitment proof.
- Preserve conflicts, source limits, confidence, and unknown fields explicitly;
  an empty provider response is inconclusive.

## Workflow

1. Call `decode_vehicle_identity` with the identifier and compact CRM vehicle
   profile. Use `decode_vehicle_identities` for a batch.
2. Call `catalog_provider_status` before claiming that a paid catalog or API is
   available.
3. For an exact requested part, call `resolve_vin_oem_parts`; require the
   production/build split and configuration fields it reports as missing.
4. Hand a stable, redacted identity and unresolved caveats to the parts-search
   workflow. Do not write an unconfirmed part as confirmed CRM data.

A registration plate may only produce an identity lead through the configured
plate-to-VIN provider route. Compare any returned VIN with the physical vehicle
and exact live CRM context; never overwrite an existing VIN from that hit alone.

## Escalation And Output

If identity is ambiguous or sources conflict, request the vehicle plate or
registration/inspection document and the relevant engine, transmission, market,
or production code. Prefer physical documents and manufacturer/EPC evidence over
generic decoders and marketplace guesses.

Return only a compact redacted vehicle profile, identifier type, market/build
clues, evidence-backed fields, conflicts, confidence, missing context, provider
status, and the next authoritative source. High-confidence identity still does
not prove fitment; the selected part needs VIN/frame-specific EPC or supplier
confirmation.
