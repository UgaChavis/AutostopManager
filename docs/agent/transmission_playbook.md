# Transmission Playbook

Purpose: working guide for complaints, diagnostics, fluids, repair planning,
and quality control on MT, AT, CVT, DCT, AMT, and heavy-duty transmission
units.

## When To Use

Use this playbook for any of these topics:

- коробка передач, КПП, АКПП, МКПП, вариатор, DSG, DCT, AMT
- сцепление, мехатроник, TCM, shift quality, harsh shift, shudder, slip
- no movement, delayed engagement, reverse-only, overheating, leaks
- clutch adaptation, transmission adaptation, calibration, programming
- transmission fluid selection, fill capacity, level check, service interval
- heavy-duty transmission or clutch service

## Mandatory Inputs

Before giving a technical answer, collect or extract:

- VIN or chassis/frame number
- market / region
- year
- make and model
- engine code
- transmission type, code, and family
- drivetrain and body/grade when they change the unit
- mileage
- complaint in the customer's own words
- conditions when the fault appears: cold, hot, load, speed, gear, reverse
- DTCs and freeze frame data if available
- service history: fluid change, overheating, towing, water ingress, prior repair

If the transmission code is missing, do not confirm fluid, adaptation, torque,
or procedure facts.

## Source Hierarchy

1. OEM service information for the exact VIN and transmission code.
2. Transmission-manufacturer documentation.
3. Component-supplier documentation.
4. Owner manuals and public training pages for theory only.
5. Forums and community posts only as hypotheses, never as final authority.

## Official And Public Routes

Use these families as the first non-OEM routing layer:

- ZF Aftermarket for passenger-car AT service, kit applicability, lubricant
  lists, and 8HP / 5HP / 6HP guidance.
- Eaton for heavy-duty manual and automated transmission manuals,
  troubleshooting, lubrication, ServiceRanger, and clutch calibration.
- Allison Transmission for service tools, publications, diagnostics, and
  training.
- AISIN and JATCO for AT / CVT / MT product family orientation and CVT data.
- Valeo, Schaeffler / LuK, and BorgWarner for DCT and clutch-system guidance.

Use `recommend_automotive_sources(data_type="transmission")` for the first
source route pass, then narrow by brand and exact unit.

## VAG DSG / Audi S Tronic Route

For Volkswagen Group DSG or Audi S tronic questions, open
`docs/agent/dsg_transmission_playbook.md` before giving software, adaptation,
fluid, mechatronic, or parts-fitment guidance.

DSG/S tronic trigger terms include:

- DSG, S tronic, DQ200, DQ250, DQ381, DQ380, DQ500, DL501, DL382
- 0AM, 0CW, 02E, 0D9, 0GC, 0BH, 0BT, 0DL, 0B5, 0CK, 0CL
- мехатроник, mechatronic, TCM, J217, J743, ODIS, SVM, Software Version
  Management, basic settings, adaptation

Rules:

- Treat OEM software updates as VIN/software-level-specific repair actions,
  not generic performance improvements.
- Separate OEM ODIS/SVM/TPI/TSB updates from TCU tuning/remap requests.
- Basic settings and clutch adaptation are diagnostic/service procedures, not
  routine resets.
- Dry-clutch DQ200 and wet-clutch DQ250/DQ381/DQ500/DL501 routes differ for
  oil, clutch, service, and failure logic.
- For used mechatronic or gearbox sourcing, require transmission code,
  hardware/software numbers, old part suffix, donor details, and coding status.

## Diagnostic Workflow

1. Record the complaint verbatim.
2. Identify the exact transmission unit and code.
3. Perform a visual inspection: leaks, mounts, connectors, harnesses,
   cooling lines, damage, contamination, overheating.
4. Scan all modules and preserve DTCs before clearing anything.
5. Verify fluid level and fluid type only by the OEM procedure.
6. Road-test with symptom reproduction and data logging.
7. Build multiple hypotheses by subsystem, not a single guess.
8. Repair only with a source-backed procedure.
9. Perform post-repair QC: level check, leak check, scan, adaptation if
   required, road test, and customer explanation.

## Fluid Rules

- ATF is not universal for CVT, DCT, or AMT.
- Never mix fluids unless the OEM explicitly allows it.
- Level checks may depend on temperature, selector position, engine state,
  or a special service sequence.
- Unknown unit or unknown fluid means no confirmed recommendation.
- Distinguish drain/refill, oil-only, oil-and-filter, dry fill, and
  cooler-line service.

## Symptom Map

| Symptom | Likely subsystems | First checks |
|---|---|---|
| No movement forward and backward | level, pump, drive, internal mechanical failure | level, DTC, mechanical link, pressure if allowed |
| Delayed engagement | pressure, fluid level, adaptation, leaks | fluid level, temperature, DTC, adaptation data |
| Harsh shift or kick | pressure, mounts, adaptation, hydraulic control | scan data, fluid state, mounts, TSBs |
| TCC shudder / speed-sensitive vibration | TCC, fluid, engine misfire, mounts | TCC command, slip, fluid spec, misfire check |
| CVT slip or overheat | fluid, pressure, cooling, belt / chain, sensors | fluid spec, temperature, cooling, debris |
| DCT shudder or launch jerk | clutch adaptation, voltage, mechatronic, flywheel | voltage, clutch data, adaptation status, DTCs |
| Manual crunch / grind | clutch release, hydraulics, lubricant, selection mechanism | clutch release, fluid, linkage, synchro signs |
| Noise that changes with speed | bearings, gears, differential, load path | road-test pattern, lift inspection, debris |
| Speed-sensor fault | sensor, wiring, trigger wheel, TCM | power, ground, signal, connector, mechanical trigger |

## Manager Checklist

- Confirm the exact unit before ordering fluid or parts.
- Record source, confidence, and caveats in the work order.
- Treat community posts as clues only.
- On repeat complaints, compare prior measurements, parts, technician notes,
  source used, and QC result before opening a new diagnostic branch.

## Safety Notes

- Hot transmission fluid and heavy assemblies can injure staff.
- Hydraulic pressure, springs, and rotating components require procedure
  discipline.
- Hybrid and EV reduction drives may involve high voltage; use OEM procedures
  only.
- If the temperature or debris condition is unsafe, stop the road test and
  reassess.
