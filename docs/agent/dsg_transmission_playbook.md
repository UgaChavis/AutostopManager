# DSG Transmission Playbook

Purpose: route VAG DSG/S tronic diagnosis, mechatronic work, basic settings,
software updates, and parts selection without guessing procedures.

## Trigger And Required Inputs

Use for DSG, S tronic, DQ200/DQ250/DQ380/DQ381/DQ500, DL501/DL382,
mechatronic, clutch adaptation, basic settings, ODIS, or SVM requests.

Collect before a specific recommendation:

- VIN, market, production date, model, engine, drivetrain, and PR codes;
- exact gearbox family/code and current hardware/software part numbers;
- full DTCs, freeze frame, coding, adaptations, and pre-scan;
- complaint conditions, repair history, fluid/service history, and battery
  support state;
- whether the request is an OEM repair/update or third-party TCU tuning.

If the family is unknown, do not confirm fluid, clutch procedure, software,
coding, or adaptation sequence.

## Family Orientation

| Family | Common codes | Distinction |
| --- | --- | --- |
| DQ200 | 0AM, 0CW | Transverse 7-speed dry clutch; separate mechatronic-fluid logic. |
| DQ250 | 02E | Transverse 6-speed wet clutch. |
| DQ380/DQ381 | 0DE, 0GC | Transverse 7-speed wet clutch; platform and market matter. |
| DQ500 | 0BH, 0BT, 0DL | Higher-torque transverse 7-speed wet clutch. |
| DL501 | 0B5 | Longitudinal Audi 7-speed wet clutch. |
| DL382 | 0CK, 0CL and variants | Longitudinal Audi 7-speed; do not reuse DL501 values. |

This table is orientation only. VIN-specific service information and the scan
report decide oil, fitment, coding, and update eligibility.

## Sources

1. Exact-VIN VAG service information and campaign applicability.
2. ODIS guided functions, official test plans, and SVM instructions.
3. Public manufacturer communications for symptom/applicability clues.
4. Genuine parts catalog for unit, clutch, mechatronic, seals, and fluid.
5. Official training material for theory.
6. Forums, VCDS/Ross-Tech, and videos only as vocabulary or hypotheses.

Source registry: `docs/agent/automotive_sources/automotive_repair_sources_catalog.json`.
Use the VAG/Audi brand route plus `data_type="transmission"`; document-level
TSBs remain applicability clues until matched to the exact VIN/software level.

## Workflow

1. Preserve the pre-scan, DTCs, software/coding, and adaptation values.
2. Identify the gearbox family and exact code.
3. Check voltage, power/ground, harness, connectors, leaks, cooling, fluid by
   OEM procedure, mounts/flywheel, and mechanical symptoms.
4. Separate hydraulic/electrical mechatronic faults, clutch wear, vibration,
   and software/adaptation state.
5. Run basic settings only through the exact OEM guided function and only when
   required by diagnosis or a repair operation.
6. Apply software only through an applicable OEM SVM/campaign action with
   stable power and the official test plan.
7. Re-scan, complete required adaptation/road test, and verify the complaint.
8. Keep source, old/new software, measurements, and caveats in internal
   workflow evidence; put only confirmed repair results in the public order.

## Software And Adaptation Boundaries

- An OEM update is tied to VIN, current software level, documented symptom,
  and action/TSB applicability; there is no generic "latest firmware" route.
- Basic settings are not a routine reset and must not hide clutch, flywheel,
  pressure, fluid, wiring, or mechatronic faults.
- Used modules may require authorized coding, software correction, component
  protection, and adaptations. Do not provide bypass or cloning instructions.
- TCU tuning/remapping is separate from OEM repair and requires an explicit
  warranty, driveline-load, legality, and drivability risk discussion.

## Parts Handoff

For sourcing, pass VIN, gearbox code, PR codes, drive type, old part number and
suffix, hardware/software numbers, dry/wet distinction, donor details,
condition, warranty, and coding status. "Подходит на DSG" is not fitment proof.

## Safety And Legal Limits

- No immobilizer/component-protection bypass, EEPROM/NVM cloning, VIN or
  odometer manipulation, emissions/DTC suppression, or unverified flash files.
- Hot fluid, rotating assemblies, and high-voltage hybrid systems require the
  exact OEM safety procedure.
- Final procedure values must come from OEM/licensed data for the exact VIN and
  gearbox.
