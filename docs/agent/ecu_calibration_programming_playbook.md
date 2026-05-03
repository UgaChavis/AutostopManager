# ECU Calibration and Programming Playbook

Purpose: route ECU programming, calibration, coding, instrument-cluster/KOMBI, and "стрелковка" questions into the local owner-provided ECU knowledge pack while preserving legal, safety, and OEM-source boundaries.

## Source Pack

- Path: `docs/agent/automotive_sources/source_cache/ecu_calibration_programming_knowledge_pack/`
- Intake source: owner-provided archive `C:/Users/User/Downloads/ecu_calibration_programming_knowledge_pack.zip`
- Created in pack README: `2026-05-03`
- Classification: owner-provided training/reference summaries, public-source routes, CSV/JSONL tables, Markdown, PDF renderings, and synthetic examples.
- License/source boundary: safe for local reference as owner-provided material; final VIN-specific programming, coding, adaptation, recovery, immobilizer, emissions, SRS, ADAS, HV, and instrument-cluster procedures must still be verified through official OEM documentation and authorized tools.

## Use This First When

- the user asks about ECU programming, flashing, calibration, coding, adaptation, UDS/OBD, ODX/A2L/CDF/DCM/MDF, BIN/HEX/S19, checksum/signature concepts, flash failure recovery, or validation logs
- the user says "стрелковка", "стрелки", "приборка", "комбинация приборов", "KOMBI", "instrument cluster", "needle sweep", "needle calibration", or "gauge cluster coding"
- the task is to understand how programming/coding workflows work, not to perform unsafe bypasses or unverified modifications
- a BMW workflow question involves ISTA/AOS/AIR, I-level, VO/FA coding, measures plan, KOMBI, DME/DDE/EGS/BDC/FEM/CAS/EWS, ICOM, or recovery after failed programming

## Source Order

1. Exact vehicle context: VIN/chassis, market, year/make/model, module, current DTC/fault memory, complaint, prior programming history, and customer request.
2. This playbook and the local ECU calibration/programming knowledge pack.
3. Model-specific skill or playbook when present, such as `bmw-f15-n63` for BMW X5 F15/N63TU.
4. OEM official programming/coding documentation, tool plan, subscription portal, and service information for the exact VIN/module.
5. Public standards and source routes listed by the pack: ASAM A2L/ODX/MDF/CDF, ISO 14229 UDS, ISO 15765 DoCAN, ISO 13400 DoIP, SAE J2534, AUTOSAR.
6. Secondary forum or field reports only as hypothesis generators, never as the authority for writing modules.

## Pack Navigation

Markdown modules:

- `md/00_scope_and_boundaries_ru.md` - legal/safety boundaries, permitted learning scope, forbidden shortcuts.
- `md/01_ecu_fundamentals_ru.md` - ECU memory layers, firmware/calibration/coding/adaptation, NVM/EEPROM, flash segments.
- `md/02_networks_uds_obd_ru.md` - CAN, DoIP, UDS, OBD, diagnostic sessions, security-access concepts, J2534.
- `md/03_file_formats_ru.md` - BIN, Intel HEX, Motorola S-record, A2L, DCM, CDF/CDFX, ODX/PDX, MDF/MF4, BLF/ASC/DBC.
- `md/04_calibration_theory_ru.md` - calibration maps, axes, units, interpolation, validation and risk boundaries.
- `md/05_oem_programming_workflow_ru.md` - precheck, power supply, programming plan, coding, adaptation, DTC/readiness handling, report content.
- `md/06_bmw_programming_overview_ru.md` - BMW ISTA/AOS/AIR workflow, I-level, VO/FA, measures plan, DME/DDE/EGS/BDC/FEM/CAS/EWS/KOMBI.
- `md/07_emissions_diagnostics_ru.md` - legal diagnostics of EGR/DPF/GPF/SCR/readiness without delete/tampering.
- `md/08_flash_failures_recovery_ru.md` - failed flash triage, recovery/resume, bootloader response, replacement-block workflow.
- `md/09_validation_logging_ru.md` - logs, road-test validation, report fields, source freshness rule.
- `md/10_service_scenarios_ru.md` - safe scenario cards for post-flash loss of power, no-response ECU, contract ECU, readiness, customer delete requests, unknown tuned file.
- `md/99_index_ru.md` - pack index.

Data indexes:

- `data/glossary_ecu_programming.jsonl` and `.csv` - ECU/programming abbreviations and Russian explanations.
- `data/uds_services_reference.csv` - UDS services by service ID and safe role.
- `data/file_format_index.csv` - format names, meaning, and caveats.
- `data/generic_dtc_examples_emissions_network.csv` - generic emissions/network DTC examples for diagnostic routing.
- `data/bmw_ecu_module_dictionary.csv` - BMW module abbreviations including KOMBI/instrument cluster.
- `data/programming_precheck_matrix.csv` - programming precheck matrix.
- `data/risk_register.jsonl` - programming/coding risk register.
- `data/repair_scenario_cards.jsonl` - scenario cards for repair knowledge workflows.
- `data/learning_flashcards.jsonl` - learning cards for concepts.
- `sources/citations_and_standards.md` and `sources/public_source_catalog.csv` - public standards/source routes.

Synthetic examples:

- `examples/` contains toy A2L, DCM, ODX, Intel HEX, S-record, calibration metadata, and MDF metadata examples for parsing and concept explanation only. They are not vehicle files and must not be used as programming input.

## Operating Rules

- Do not provide or execute instructions for emissions deletes, DTC/MIL/readiness suppression, immobilizer bypass, seed-key/security-access bypass, signature/checksum circumvention, mileage/odometer tampering, or copying unknown EEPROM/NVM dumps into road vehicles.
- Treat "стрелковка" as an instrument-cluster/KOMBI/coding/adaptation topic first. Clarify the intended legal action: needle sweep/display coding, pointer test, cluster replacement coding, or diagnostics. If the request touches odometer, EEPROM, VIN, immobilizer, or hidden tamper data, stop and route to official/legal service procedure only.
- For BMW KOMBI questions, use the pack to understand vocabulary and workflow, then verify exact actions through BMW ISTA/AIR/AOS, vehicle order VO/FA, integration level, and VIN-specific measures plan.
- Never infer compatibility from file extension alone. Validate module hardware number, software compatibility, market/emissions variant, transmission/drivetrain, signature, and tool plan.
- Before programming, require stable service power supply, full pre-scan, saved identifiers, correct VCI/tool version, no active power/network/immobilizer blockers, and a recovery path.
- After programming/coding, require post-scan, required adaptations, road-test/log validation, readiness handling where relevant, and a report with VIN, mileage, reason, old/new software IDs, source package, voltage, VCI, pre/post results, and adaptations.
- Use this pack for education and routing. For exact write steps, coding values, security procedures, legal compliance, or VIN-specific module replacement, use official OEM data.

## Search Examples

```powershell
python -m autostop_manager.cli knowledge-probe "стрелковка KOMBI BMW приборка coding"
python -m autostop_manager.cli knowledge-search "KOMBI coding комбинация приборов" --domain ecu_calibration_programming
python -m autostop_manager.cli knowledge-search "BMW I-level VO FA measures plan" --domain ecu_calibration_programming
python -m autostop_manager.cli knowledge-search "ECU не отвечает после flash recovery" --domain ecu_calibration_programming
python -m autostop_manager.cli knowledge-search "A2L DCM ODX calibration format" --domain ecu_calibration_programming
```
