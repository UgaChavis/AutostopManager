# ECU Calibration and Programming Playbook

Canonical route for ECU programming, calibration, coding and
instrument-cluster/KOMBI questions. The compact local glossary and file-format
index are educational aids only; exact VIN/module work requires official OEM
documentation and authorized tools.

## Use This First When

- ECU programming, flashing, calibration, coding, adaptation, UDS/OBD,
  ODX/A2L/CDF/DCM/MDF, BIN/HEX/S19, recovery or validation.
- "стрелковка", приборка, KOMBI, instrument cluster, needle sweep or gauge
  coding.
- BMW ISTA/AOS/AIR, I-level, VO/FA, measures plan, ICOM or failed-programming
  recovery.

## Source Order

1. Exact vehicle context: VIN/chassis, market, year/make/model, module, current DTC/fault memory, complaint, prior programming history, and customer request.
2. This playbook, glossary and file-format index.
3. Model-specific route or playbook when present, such as the BMW F15/N63 route
   for BMW X5 F15/N63TU.
4. OEM official programming/coding documentation, tool plan, subscription portal, and service information for the exact VIN/module.
5. Official ASAM/ISO/SAE/AUTOSAR overview or licensed standard as needed.
6. Secondary forum or field reports only as hypothesis generators, never as the authority for writing modules.

## Local Index

- `docs/agent/automotive_sources/source_cache/ecu_calibration_programming_knowledge_pack/data/glossary_ecu_programming.jsonl`
- `docs/agent/automotive_sources/source_cache/ecu_calibration_programming_knowledge_pack/data/file_format_index.csv`

## Operating Rules

- Do not provide or execute instructions for emissions deletes, DTC/MIL/readiness suppression, immobilizer bypass, seed-key/security-access bypass, signature/checksum circumvention, mileage/odometer tampering, or copying unknown EEPROM/NVM dumps into road vehicles.
- Treat "стрелковка" as an instrument-cluster/KOMBI/coding/adaptation topic first. Clarify the intended legal action: needle sweep/display coding, pointer test, cluster replacement coding, or diagnostics. If the request touches odometer, EEPROM, VIN, immobilizer, or hidden tamper data, stop and route to official/legal service procedure only.
- For BMW KOMBI questions, use the pack to understand vocabulary and workflow, then verify exact actions through BMW ISTA/AIR/AOS, vehicle order VO/FA, integration level, and VIN-specific measures plan.
- Never infer compatibility from file extension alone. Validate module hardware number, software compatibility, market/emissions variant, transmission/drivetrain, signature, and tool plan.
- Before programming, verify VIN, hardware/software compatibility, official
  package and coding/adaptation plan; require stable service power, full
  pre-scan, saved identifiers, correct VCI/tool version, healthy network and an
  official recovery path.
- After programming/coding, require post-scan, required adaptations, road-test/log validation, readiness handling where relevant, and a report with VIN, mileage, reason, old/new software IDs, source package, voltage, VCI, pre/post results, and adaptations.
- Use this pack for education and routing. For exact write steps, coding values, security procedures, legal compliance, or VIN-specific module replacement, use official OEM data.

## Search

```bash
.venv/bin/python -m autostop_manager.cli knowledge-probe "стрелковка KOMBI BMW приборка coding"
.venv/bin/python -m autostop_manager.cli knowledge-search "KOMBI coding комбинация приборов" --domain ecu_calibration_programming
.venv/bin/python -m autostop_manager.cli knowledge-search "A2L DCM ODX calibration format" --domain ecu_calibration_programming
```
