# Toyota GR Yaris Playbook

Purpose: route Toyota GR Yaris identity, maintenance, diagnostics, recall, and
OEM-parts work without treating another Yaris variant or a public catalog as
VIN-specific service evidence.

## Trigger And Identity

Use for GR Yaris, Yaris GR, GRMN Yaris, `GXPA16`, `G16E-GTS`, `GR-FOUR`,
`EA67F`, `UC80F`, GR-DAT, or GR Yaris parts. Do not confuse GR Yaris with
ordinary Yaris, Yaris Cross, or Yaris GR Sport.

Before a final fluid, repair, campaign, or part conclusion, establish:

- VIN or Japan frame number, market, production date, model code, and grade;
- engine, transmission, drivetrain, and option/LSD package;
- for diagnostics: mileage, complaint, DTCs, freeze frame, scan data, and
  service/modification history.

`GXPA16`, `G16E-GTS`, and `GR-FOUR` are useful orientation keys. Manual and
8-speed GR-DAT routes, capacities, options, and campaign applicability still
depend on market and production date.

## Source Order

1. Exact-market Toyota service portal or dealer EPC/service information by
   VIN/frame.
2. Exact-market owner manual and maintenance data.
3. Official campaign/recall lookup by VIN/frame.
4. Licensed professional database when OEM access is unavailable.
5. Public EPC-style catalog only for candidate numbers; verify fitment through
   Toyota EPC/dealer.

Official routes:

- Toyota-Tech Europe: `https://www.toyota-tech.eu/`
- Toyota Manuals Australia: `https://toyotamanuals.com.au/`
- Toyota Japan manuals and production-period selector: `https://manual.toyota.jp/`
- Toyota Japan recall lookup: `https://www.toyota.co.jp/recall-search/dc/search`
- Toyota UK recall lookup:
  `https://www.toyota.co.uk/owners/vehicle-information/recalls`
- Toyota Australia recalls: `https://www.toyota.com.au/toyota-recalls`

Related-model data may help with theory, but it does not establish a GR Yaris
procedure or fitment.

## Working Rules

### Fluids And Maintenance

- Identify the exact unit and service operation before quoting a specification
  or capacity.
- Separate drain/refill, filter change, and dry-fill values; follow every
  market-specific footnote.
- Do not infer manual, automatic, transfer, differential, coolant, or brake
  data from another year, option package, or market.
- Use `docs/agent/fluid_maintenance_playbook.md` for the common output contract.

### Recalls And Diagnostics

- Check campaign status by VIN/frame; model name or an online bulletin alone is
  not proof that a vehicle is affected.
- Preserve DTC text, freeze frame, calibration level, measured values, and
  modification history before recommending parts or software.
- Treat forum TSB references and copied PDFs as search clues until matched to
  Toyota service information.
- Brakes, ADAS, SRS, wiring, immobilizer, and ECU programming require the exact
  OEM/licensed procedure.

### OEM Parts

1. Resolve the candidate through Toyota EPC/dealer by VIN or Japan frame.
2. Cross-check public catalogs only after exact model, production month,
   engine, transmission, grade/options, side, and axle are known.
3. Search suppliers only after the OEM candidate and applicability are stable.

In the owner report, identify the evidence and remaining VIN/frame check. In a
public CRM description, write only the confirmed compact result allowed by
`docs/agent/crm_card_description_standard.md`.

## No-Invention Rule

Never invent torque, labor time, bulletin text, wiring pinouts, fluid procedure,
ADAS/SRS steps, calibration, or part applicability. If document-level proof is
missing, say:

`Требуется проверка по OEM-сервисной информации для конкретного VIN.`
