# BMW Repair Playbook

Purpose: route general BMW repair, diagnostics, DTC, chassis, body electronics, xDrive, ZF transmission, HV, and fluid-maintenance questions into the local BMW repair knowledge pack without treating it as a closed OEM manual.

## Source Pack

- Path: `docs/agent/automotive_sources/source_cache/bmw_repair_knowledge_pack/`
- Intake source: owner-provided archive; original user download path is
  historical and not part of active routing.
- Created in pack manifest: `2026-05-03T10:32:46Z`
- Classification: compact owner-provided reference pack retained as README,
  manifest, and JSONL indexes used by active routing. Long Markdown modules
  were migrated into this playbook and removed during documentation reduction.
- License/source boundary: safe for local reference as owner-provided material; still verify final repair procedures, wiring, torque, coding, programming, campaigns, and fluid capacities by VIN in BMW ISTA/AIR/ETK/AOS/TIS or official/public bulletins.

## Use This First When

- the user asks general BMW repair or diagnostic questions without a more specific model route
- the topic is BMW DTC/fault memory, OBD II, ISTA/AIR/TIS source routing, control units, bus/network architecture, body electronics, battery drain, xDrive, ZF automatic transmission, chassis/brakes/steering, HV safety, or BMW fluids
- the vehicle is BMW but not specifically covered by the BMW F15/N63 route or another model-specific route

## Source Order

1. Exact VIN/model/year/market context from CRM or owner.
2. Model-specific route/playbook if present, such as the BMW F15/N63 route in
   `knowledge_map.json`.
3. This BMW repair playbook and the retained JSONL indexes in the local BMW
   repair knowledge pack.
4. BMW official sources: ISTA, AIR, ETK, AOS, BMW TIS, BMW owner manual by VIN, BMW recall/technical update lookup.
5. Public official documents: NHTSA BMW SIB/recall PDFs, ZF official service information, SAE/OBD standards.
6. Secondary field reports only as hypothesis generators.

## Pack Navigation

Retained pack files:

- `README_ru.md` - provenance, source boundary, and load policy.
- `manifest.json` - retained-file list and compacted-pack metadata.

Data indexes:

- `data/bmw_chassis_codes.jsonl` - BMW chassis/body-code lookup hints.
- `data/bmw_control_units_glossary.jsonl` - BMW module abbreviations and diagnostic meaning.
- `data/bmw_engine_families.jsonl` - engine families and common service topics.
- `data/bmw_fault_memory_public_examples.jsonl` - public BMW fault-memory
  examples from bulletins, including searchable DTC/fault-memory examples.
- `data/bmw_fluids_specification_logic.jsonl` - BMW fluid-specification decision logic and verification source.
- `data/bmw_symptom_diagnostic_index.jsonl` - symptom-to-system and data-to-capture map.
- `data/bmw_transmission_families.jsonl` - BMW/ZF transmission families.

## Operating Rules

- Do not give final torque specs, wiring pinouts, repair steps, programming/coding steps, ADAS/SRS/HV procedures, or exact capacities from this pack alone.
- For fluids, use this pack to choose the source route and required context; final approval/capacity must come from VIN-specific BMW/ZF/owner-manual/ISTA data.
- For DTCs, prefer BMW hex fault memory and module name over generic OBD P-codes. Generic P-codes are entry points only.
- For electrical/network faults, check battery/IBS/terminal state, grounds, water ingress, sleep/wake state, and gateway/module communication before replacing modules.
- For xDrive shudder, capture tire sizes/tread depth, VTG faults, DSC wheel speeds, service history, transfer-case oil status, and calibration state.
- For HV/EV/PHEV, do not provide hazardous procedure steps; require qualified HV safety procedure and official BMW documentation.

## Search Examples

```powershell
python -m autostop_manager.cli knowledge-search "BMW battery drain sleep IBS"
python -m autostop_manager.cli knowledge-search "BMW xDrive shudder transfer case"
python -m autostop_manager.cli knowledge-search "BMW N63 oil consumption"
python -m autostop_manager.cli knowledge-search "BMW ZF 8HP oil level"
python -m autostop_manager.cli knowledge-search "BMW fault memory 8013FE"
```
