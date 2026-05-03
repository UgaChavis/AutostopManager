# DSG Transmission Playbook

Purpose: route VAG DSG/DQ complaints, mechatronic work, basic settings, and
software-update questions without guessing procedures or treating forum advice
as service data.

## When To Use

Use this playbook for:

- DSG, S tronic, DQ200, DQ250, DQ381, DQ500, DL501, and related VAG DCT units.
- мехатроник, clutch adaptation, basic settings, kiss point, gear engagement,
  shudder, launch jerk, harsh shift, no reverse, PRNDS flashing, limp mode.
- ODIS, SVM, software update, programming campaign, coding/adaptation after
  mechatronic or clutch repair.

## Mandatory Inputs

Collect before giving a specific recommendation:

- VIN, market, year, make, model, engine code, drivetrain.
- Transmission code/family from PR codes, scan data, label, or VIN-specific
  catalog route.
- DTCs from all relevant modules with freeze frame.
- Current TCM/mechatronic hardware part number, software part number/version,
  coding, and adaptation status when programming is involved.
- Fluid type/service history for wet-clutch units.
- Battery/power-supply state before any basic settings or software update.
- Repair history: clutch, flywheel, mechatronic, wiring, fluid, water ingress,
  used unit, prior adaptation/reset.
- Whether the request is an OEM repair/update or performance TCU tuning.

If the DSG family is unknown, do not confirm fluid, clutch procedure, software
path, or adaptation sequence.

## DSG Family Map

| Family | Common codes | Layout | Clutch/oil distinction | Routing notes |
| --- | --- | --- | --- | --- |
| DQ200 | 0AM, 0CW | transverse 7-speed | dry dual clutch, separate mechatronic hydraulics | Sensitive to software level, clutch wear values, basic settings, and mechatronic variant. Do not apply wet-DSG fluid logic. |
| DQ250 | 02E | transverse 6-speed | wet dual clutch | Fluid/filter service, mechatronic data, and adaptation state matter. Common Golf/Jetta/Passat/GTI-era route. |
| DQ380 / DQ381 | 0DE, 0GC | transverse 7-speed | wet dual clutch | MQB-era route; identify market, PR codes, drivetrain, and exact gearbox code. |
| DQ500 | 0BH, 0BT, 0DL | transverse 7-speed high torque | wet dual clutch | Higher-torque vans/SUV/performance route; confirm AWD package and cooler/service variant. |
| DL501 | 0B5 | longitudinal 7-speed Audi S tronic | wet dual clutch | Use Audi TSB/TPI/erWin route for J217/J743 software and mechatronic replacement adaptations. |
| DL382 | 0CK, 0CL and related | longitudinal 7-speed Audi S tronic | wet dual clutch | VIN/software-level specific; do not reuse DL501 values. |

The family map is orientation only. Final fitment, oil, coding, and update
eligibility require VIN-specific service information and the scan report.

## Module And Software Model

- The mechatronic unit combines hydraulic control, sensors/actuators, valve
  body functions, and transmission control electronics.
- Volkswagen/Audi documents may refer to J217 transmission control module or
  J743 mechatronic control module depending on platform and document.
- A "same DSG" description is not enough for software decisions. Match vehicle,
  gearbox code, hardware part number, software part number/version, and official
  action/TPI/TSB applicability.
- Replacement mechatronic units can require coding, software-level correction,
  basic settings, adaptation drives, and legal security/component-protection
  handling. Route these to OEM service procedure.

## Source Order

1. OEM VAG service information for the exact VIN/transmission code.
2. ODIS guided functions, campaign/SVM instructions, and official test plans.
3. Public NHTSA copies of VW/Audi manufacturer communications for examples of
   symptoms, applicability, and SVM/update discipline.
4. Genuine parts/catalog route for unit, clutch kit, mechatronic, seals, and
   fluid applicability.
5. VW/Audi official technology/training material for theory only.
6. Forums, VCDS/Ross-Tech, and videos only as vocabulary or symptom clues,
   never as final authority for flash files, coding, or fitment.

Local source registry:

- `docs/agent/automotive_sources/dsg_transmission_sources.json` - official and
  public DSG/S tronic source routes, legal boundaries, and routing rules.

## Workflow

1. Preserve DTCs, freeze frame, software version, coding, and adaptation status.
2. Identify the exact DSG family before deciding whether the unit is dry-clutch
   or wet-clutch.
3. Check battery voltage, power/ground, harness, connector, fluid leaks, cooling
   and mechanical symptoms before software conclusions.
4. For DQ200/DQ250 mechatronic complaints, separate hydraulic/electrical failure,
   clutch wear, flywheel/engine vibration, and software/adaptation state.
5. Use ODIS guided functions for basic settings/adaptation. Do not improvise
   timing, pedal, temperature, or selector steps.
6. Use SVM or official campaign instructions for software updates. Do not flash
   unverified files or clone unknown dumps.
7. After repair/update, save post-scan, run required basic settings, road-test
   with measured values, and record source/caveats in the work order.

## OEM Software Update Route

Use this route when the complaint is shift quality, jerk/bump, hesitation,
emergency mode, start authorization after mechatronic replacement, or a known
campaign/update:

1. Confirm exact vehicle and DSG/S tronic family.
2. Save a full pre-scan and current TCM/J217/J743 software data.
3. Check VIN applicability in erWin/Elsa/ODIS, campaign/action screen, TPI/TSB,
   or NHTSA public TSB copy.
4. Confirm the update is an OEM SVM/ODIS action for that VIN/software level,
   not a generic "latest firmware" request.
5. Use a stable battery support unit, hardwired diagnostic interface, updated
   ODIS/tester, and the official test plan.
6. Complete SVM end to end so the server records the response.
7. Perform only the basic settings/adaptation steps required by the OEM test
   plan. Road test and re-scan after the update.
8. Record old/new part numbers, software versions, action code/TSB reference,
   DTC status, adaptation status, and road-test result in the repair order.

OEM software updates are repair actions tied to VIN, software level, and
documented symptoms. TCU tuning/remap for launch control, increased clutch
pressure, torque-limit changes, or shift-character changes is a separate risk
conversation: warranty, drivetrain load, inspections, drivability, and legal
status.

## Basic Settings And Adaptation Rules

Basic settings are a diagnostic/service step, not a routine reset. Use them
when an OEM guided function, TSB/TPI, repair operation, or symptom diagnosis
requires it.

Before basic settings:

- transmission and engine must be at the required temperature
- ECM/TCM fault memory must be addressed as required by the procedure
- vehicle must meet selector, brake, idle, and voltage requirements
- clutch/mechatronic mechanical faults must not be hidden by clearing values

After basic settings:

- follow the specified park/drive/reverse cycling and road-test plan
- verify all gears, launch behavior, reverse, hot idle, and no repeat DTCs
- document adaptation values when available

Do not use adaptation reset as a quick cure for a worn clutch, contaminated wet
clutch oil, dual-mass flywheel issue, pressure fault, wiring fault, or failing
mechatronic. It can temporarily change feel and obscure the root cause.

## Common Routes

| Request | First Route | Notes |
| --- | --- | --- |
| `DQ250 обновление ПО ODIS SVM` | OEM service info, ODIS SVM, campaign check | Needs VIN, module software, fault context, charger |
| `DQ200 мехатроник basic settings` | ODIS guided function, exact DQ200 variant | Verify battery, DTCs, clutch/mechatronic history |
| `DSG пинается после ремонта` | DTC/freeze frame, adaptation status, clutch/flywheel checks | Do not reset adaptations blindly |
| `масло DSG` | exact unit and VIN-specific fluid route | DQ200 dry-clutch logic differs from wet-clutch units |
| `контрактный мехатроник` | parts/OEM compatibility, coding/adaptation, immobilizer/component protection route | Avoid EEPROM/VIN/security bypass steps |

## Symptom Routing

| Complaint | First DSG checks | Notes |
| --- | --- | --- |
| Launch shudder / judder | clutch adaptation values, clutch wear, mounts, flywheel, oil state, DTCs | Dry DQ200 and wet DQ250/DQ381 routes differ. |
| Harsh downshift or bump to stop | TSB/TPI by VIN, TCM/J743 software, adaptations, mounts, fluid condition | Audi 0B5 public TSBs show software can be the fix for defined complaints. |
| Delayed engagement D/R | pressure/mechatronic data, fluid level, clutch adaptation, selector data | Do not condemn clutch without scan and fluid/mechatronic checks. |
| Gearbox warning/emergency mode | full scan, TCM faults, power/ground/CAN, mechatronic temperature and pressure | Software campaigns may exist, but DTC diagnosis comes first. |
| After mechatronic replacement | coding, software level, SVM action, start authorization, basic settings | Use exact Audi/VW procedure; old/new SW mismatch is common risk. |
| Rattle at idle | flywheel, clutch pack, mechatronic/software TSBs, engine idle quality | Separate transmission rattle from engine/mount noise. |

## Parts And Krasnoyarsk Search Handoff

When DSG parts sourcing is needed, hand off to `parts_sourcing` with:

- VIN, gearbox family/code, engine code, drive type, PR codes when available
- old unit/mechatronic/clutch/flywheel part number and suffix
- software/hardware numbers for mechatronic/TCM modules
- wet versus dry clutch distinction
- new/used/contract acceptance, warranty expectation, and whether coding is
  included by the seller

For used mechatronic/gearbox listings, never treat "подходит на DSG" as enough.
Demand part-number suffix, donor VIN/model/year/engine/transmission code,
mileage proof, warranty, and whether it is locked/coded/adapted.

## Safety And Legal Limits

- Do not provide immobilizer, component-protection, EEPROM/NVM cloning, VIN
  tampering, odometer, or security-bypass instructions.
- Do not suggest emissions, DTC, readiness, or diagnostic-monitor deletion.
- Treat third-party flash files as unsafe unless verified through official or
  licensed service channels.
- For a real vehicle, final procedure details must come from OEM/licensed
  service information for the exact VIN and unit.
