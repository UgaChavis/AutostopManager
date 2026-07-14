# ECU Calibration and Programming Playbook

Purpose: route ECU programming, calibration, coding, instrument-cluster/KOMBI, and "стрелковка" questions into the local owner-provided ECU knowledge pack while preserving legal, safety, and OEM-source boundaries.

## Source Pack

- Path: `docs/agent/automotive_sources/source_cache/ecu_calibration_programming_knowledge_pack/`
- Intake source: owner-provided archive; original user download path is
  historical and not part of active routing.
- Created in pack README: `2026-05-03`
- Classification: compact owner-provided training/reference route retained as
  README, MANIFEST, public-source routes, and active CSV/JSONL tables. Long
  Markdown modules were migrated into this playbook and removed during
  documentation reduction.
- License/source boundary: safe for local reference as owner-provided material; final VIN-specific programming, coding, adaptation, recovery, immobilizer, emissions, SRS, ADAS, HV, and instrument-cluster procedures must still be verified through official OEM documentation and authorized tools.

## Use This First When

- the user asks about ECU programming, flashing, calibration, coding, adaptation, UDS/OBD, ODX/A2L/CDF/DCM/MDF, BIN/HEX/S19, checksum/signature concepts, flash failure recovery, or validation logs
- the user says "стрелковка", "стрелки", "приборка", "комбинация приборов", "KOMBI", "instrument cluster", "needle sweep", "needle calibration", or "gauge cluster coding"
- the task is to understand how programming/coding workflows work, not to perform unsafe bypasses or unverified modifications
- a BMW workflow question involves ISTA/AOS/AIR, I-level, VO/FA coding, measures plan, KOMBI, DME/DDE/EGS/BDC/FEM/CAS/EWS, ICOM, or recovery after failed programming

## Source Order

1. Exact vehicle context: VIN/chassis, market, year/make/model, module, current DTC/fault memory, complaint, prior programming history, and customer request.
2. This playbook and the retained tables/source catalog in the local ECU
   calibration/programming knowledge pack.
3. Model-specific route or playbook when present, such as the BMW F15/N63 route
   for BMW X5 F15/N63TU.
4. OEM official programming/coding documentation, tool plan, subscription portal, and service information for the exact VIN/module.
5. Public standards and source routes listed by the pack: ASAM A2L/ODX/MDF/CDF, ISO 14229 UDS, ISO 15765 DoCAN, ISO 13400 DoIP, SAE J2534, AUTOSAR.
6. Secondary forum or field reports only as hypothesis generators, never as the authority for writing modules.

## Pack Navigation

Retained pack files:

- `README.md` - provenance, purpose, and safety boundary.
- `docs/agent/automotive_sources/source_cache/ecu_calibration_programming_knowledge_pack/MANIFEST.md`
  - retained-file list and compacted-pack metadata.
Data indexes:

- `docs/agent/automotive_sources/source_cache/ecu_calibration_programming_knowledge_pack/data/glossary_ecu_programming.jsonl` - ECU/programming abbreviations and Russian explanations.
- `docs/agent/automotive_sources/source_cache/ecu_calibration_programming_knowledge_pack/data/uds_services_reference.csv` - UDS services by service ID and safe role.
- `docs/agent/automotive_sources/source_cache/ecu_calibration_programming_knowledge_pack/data/file_format_index.csv` - format names, meaning, and caveats.
- `docs/agent/automotive_sources/source_cache/ecu_calibration_programming_knowledge_pack/data/generic_dtc_examples_emissions_network.csv` - generic emissions/network DTC examples for diagnostic routing.
- `docs/agent/automotive_sources/source_cache/ecu_calibration_programming_knowledge_pack/data/bmw_ecu_module_dictionary.csv` - BMW module abbreviations including KOMBI/instrument cluster.
- `docs/agent/automotive_sources/source_cache/ecu_calibration_programming_knowledge_pack/data/programming_precheck_matrix.csv` - programming precheck matrix.
- `docs/agent/automotive_sources/source_cache/ecu_calibration_programming_knowledge_pack/data/risk_register.jsonl` - programming/coding risk register.
- `docs/agent/automotive_sources/source_cache/ecu_calibration_programming_knowledge_pack/data/repair_scenario_cards.jsonl` - scenario cards for repair knowledge workflows.
- `docs/agent/automotive_sources/source_cache/ecu_calibration_programming_knowledge_pack/sources/public_source_catalog.csv` - public standards and official source routes.

## Operating Rules

- Do not provide or execute instructions for emissions deletes, DTC/MIL/readiness suppression, immobilizer bypass, seed-key/security-access bypass, signature/checksum circumvention, mileage/odometer tampering, or copying unknown EEPROM/NVM dumps into road vehicles.
- Treat "стрелковка" as an instrument-cluster/KOMBI/coding/adaptation topic first. Clarify the intended legal action: needle sweep/display coding, pointer test, cluster replacement coding, or diagnostics. If the request touches odometer, EEPROM, VIN, immobilizer, or hidden tamper data, stop and route to official/legal service procedure only.
- For BMW KOMBI questions, use the pack to understand vocabulary and workflow, then verify exact actions through BMW ISTA/AIR/AOS, vehicle order VO/FA, integration level, and VIN-specific measures plan.
- Never infer compatibility from file extension alone. Validate module hardware number, software compatibility, market/emissions variant, transmission/drivetrain, signature, and tool plan.
- Before programming, require stable service power supply, full pre-scan, saved identifiers, correct VCI/tool version, no active power/network/immobilizer blockers, and a recovery path.
- After programming/coding, require post-scan, required adaptations, road-test/log validation, readiness handling where relevant, and a report with VIN, mileage, reason, old/new software IDs, source package, voltage, VCI, pre/post results, and adaptations.
- Use this pack for education and routing. For exact write steps, coding values, security procedures, legal compliance, or VIN-specific module replacement, use official OEM data.

## Search Examples

Linux/Codex shell:

```bash
.venv/bin/python -m autostop_manager.cli knowledge-probe "стрелковка KOMBI BMW приборка coding"
.venv/bin/python -m autostop_manager.cli knowledge-search "KOMBI coding комбинация приборов" --domain ecu_calibration_programming
.venv/bin/python -m autostop_manager.cli knowledge-search "BMW I-level VO FA measures plan" --domain ecu_calibration_programming
.venv/bin/python -m autostop_manager.cli knowledge-search "ECU не отвечает после flash recovery" --domain ecu_calibration_programming
.venv/bin/python -m autostop_manager.cli knowledge-search "A2L DCM ODX calibration format" --domain ecu_calibration_programming
```

PowerShell:

```powershell
python -m autostop_manager.cli knowledge-probe "стрелковка KOMBI BMW приборка coding"
python -m autostop_manager.cli knowledge-search "KOMBI coding комбинация приборов" --domain ecu_calibration_programming
python -m autostop_manager.cli knowledge-search "BMW I-level VO FA measures plan" --domain ecu_calibration_programming
python -m autostop_manager.cli knowledge-search "ECU не отвечает после flash recovery" --domain ecu_calibration_programming
python -m autostop_manager.cli knowledge-search "A2L DCM ODX calibration format" --domain ecu_calibration_programming
```
