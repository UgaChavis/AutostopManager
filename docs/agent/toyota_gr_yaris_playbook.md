# Toyota GR Yaris Playbook

Purpose: make AutostopManager useful and strict for Toyota GR Yaris repair,
maintenance, fluids, recalls, technical bulletins, diagnostics, and OEM parts
lookup without copying closed Toyota service data.

## Trigger

Use this playbook when the request mentions Toyota GR Yaris / Yaris GR,
GRMN Yaris, GXPA16, G16E-GTS, GR-FOUR, 6MT EA67F, 8AT UC80F, RZ, RC,
High Performance, Circuit Pack, Performance Pack, or GR Yaris OEM parts.

Do not confuse the GR Yaris with normal Yaris, Yaris Cross, or Yaris GR Sport.

## Required Identity

Before giving final repair, fluid, recall, or part-number conclusions, collect:

- VIN or Japanese chassis/frame number
- market / region
- production year and production month where possible
- full model code and grade
- engine code
- transmission type/code
- drivetrain and LSD/front-rear diff package
- mileage, complaint, DTCs, and scan results for diagnostics

For Japan-market cars, the chassis/frame number and full model code are often
more useful than an ISO VIN route. For EU/UK/AU imports, use the VIN and local
Toyota portal or dealer route.

## Durable Vehicle Facts

- Main GR Yaris performance model route: `GXPA16`.
- Engine: `G16E-GTS`, 1.6 L in-line 3-cylinder direct-injection turbo.
- Drivetrain: `GR-FOUR` AWD with electronically controlled multi-plate clutch.
- Transmission routes: early cars are mainly 6-speed iMT/manual; 2024+ markets
  add 8-speed GR-DAT automatic.
- Australia 2025 spec table lists transmission codes `EA67F` for 6MT and
  `UC80F` for 8AT.
- Toyota UK technical spec lists optional front/rear Torsen LSD on early UK
  cars; do not assume LSD without grade/options/VIN.

Sources: Toyota Global Newsroom, Toyota Japan specs, Toyota UK spec PDF, Toyota
Australia GR Yaris spec table.

## Source Order

1. Toyota official service portal for the exact market and VIN/frame:
   Toyota-Tech Europe, Toyota TIS where market-covered, Toyota Manuals
   Australia, or local Toyota dealer EPC/service information.
2. Toyota public owner manual / maintenance data for exact market/year.
3. Official recall/campaign portal and government recall dataset.
4. Licensed professional database when OEM access is unavailable.
5. Public EPC-style catalogs only as part-number clues; verify final fitment
   through Toyota EPC/dealer by VIN or frame.

## Official Source Routes

- Toyota-Tech Europe: `https://www.toyota-tech.eu/`
  - Use for EU repair manual, wiring, TSB, calibration, and model-code search.
  - Toyota states ECU updates and bulletin applicability are VIN/model-code
    specific; do not apply software updates without the relevant bulletin.
- Toyota TIS North America: `https://techinfo.toyota.com/`
  - Use for official Toyota service information where the vehicle/market is
    covered. GR Yaris was not a US-market model, but related G16E-GTS GR
    Corolla data may help theory only, not final GR Yaris procedure.
- Toyota Manuals Australia: `https://toyotamanuals.com.au/`
  - Use for Australia/RHD service and warranty publications.
- Toyota Japan owner manuals: `https://manual.toyota.jp/`
  - Use for Japan-market owner-level maintenance data.
- Toyota Japan GR Yaris maintenance page:
  `https://manual.toyota.jp/gr_yaris/2408/cv/ja_JP/contents/vhch08se010401.php`
- Toyota Japan recalls: `https://toyota.jp/recall/`
- Toyota Japan recall/frame lookup:
  `https://www.toyota.co.jp/recall-search/dc/search`
- Toyota UK recall checker:
  `https://www.toyota.co.uk/help-centre/recalls`
- Toyota Australia recall checker:
  `https://www.toyota.com.au/toyota-recalls`

## Fluids And Capacities

For 2024+ Japan-market GR Yaris GXPA16, Toyota's public maintenance page lists:

- Fuel: premium unleaded gasoline, 50 L reference capacity.
- Engine oil: Toyota Genuine Motor Oil SP 0W-20, API SP/RC, ILSAC GF-6A;
  4.0 L oil-only, 4.3 L oil and filter.
- Coolant: Toyota Super Long Life Coolant; the table separates automatic and
  manual cars and sub-radiator/non-sub-radiator cases. Treat the values as
  market/year-specific and verify the footnotes before quoting a final amount.
- Automatic transmission: Toyota Genuine Auto Fluid WS, 7.4 L reference
  capacity for 8AT; Toyota says to consult a dealer when replacement is needed.
- Manual transmission: Toyota Genuine Manual Transmission Gear Oil LV 75W or
  API GL-4 SAE 75W equivalent; 2.0 L with LSD, 2.1 L without LSD.
- Transfer: Toyota Genuine Differential Gear Oil LT, API GL-5 SAE 75W-85,
  0.45 L.
- Rear differential: Toyota Genuine Differential Gear Oil LX, API GL-5
  SAE 75W-85, 0.5 L.
- Clutch and brake fluid: Toyota Genuine Brake Fluid DOT4 CLASS6; Toyota
  Brake Fluid 2500H-A is listed as fallback when DOT4 CLASS6 is unavailable.
- Wheel nut torque is visible in the public owner manual, but for shop repair
  instructions still prefer the OEM repair manual for the exact operation.

Important: these values are not universal for every year/market. Before a
repair order or parts/fluid estimate, verify VIN/frame, model year, transmission,
LSD, grade, sub-radiator, and market.

## Recalls And Campaigns

Always check by VIN/frame before delivery or diagnosis.

Public examples to remember as search routes:

- Australia recall `REC-005068` / campaign `XGG08` included GR Yaris 2020-2021
  among affected Toyota C-HR/Yaris variants for front radar sensor calibration.
- Japan recall `5339`, started June 23, 2023, covered some GR Yaris GRMN /
  GRMN Circuit Package `4BA-GXPA16` vehicles for backdoor waterproof cap and
  related electrical/water ingress risk.
- Japan recall `5694`, started July 17, 2025, covered some `4BA-GXPA16`
  GR Yaris vehicles for combination-meter program/countermeasure work.

Do not assume a vehicle is affected from model name alone; use the official
VIN/frame recall portal or dealer check.

## TSB And Diagnostics

- Treat P023400 / overboost bulletin references found in forums or copied PDFs
  as clues only. Confirm the bulletin ID and repair procedure in Toyota-Tech,
  Toyota TIS where applicable, or dealer service information.
- For engine/turbo complaints, collect DTC freeze frame, boost target/actual,
  wastegate control data, intake leaks, exhaust modifications, calibration
  history, fuel quality, and service history before recommending parts.
- For AWD noises or overheating, separate MT/front diff, transfer, rear diff,
  coupling control, tires/circumference mismatch, and track-use history.
- For brakes/suspension/ADAS/SRS/wiring/ECU programming, use OEM/licensed
  procedure only.

## OEM Parts Route

Preferred path:

1. Toyota EPC/dealer by VIN or Japan frame number.
2. Public cross-check by model/frame: Nengun, Amayama, MegaZip, PartSouq,
   7zap.
3. Supplier/marketplace sourcing after OEM number is stable: local Toyota
   dealer, Krasnoyarsk suppliers, Drom, ZZap, Avito, Emex, Exist, Autodoc.

Cross-check public catalogs with:

- model code `GXPA16`
- engine `G16E-GTS`
- grade `RZ`, `RC`, `GRMN`, High Performance/Circuit/Performance Pack
- transmission `EA67F` or `UC80F`
- side/axle/body color/interior trim when relevant
- production month and market

Return every OEM number with source URL, market, fitment confidence, and what
still needs VIN/frame confirmation.

## No-Invention Rule

Never invent torque specs, labor time, TSB text, wiring pinouts, fluid
procedures, ADAS calibration, SRS steps, immobilizer/key procedures, ECU
calibration, or original part applicability.

If document-level verification is missing, say:

`Требуется проверка по OEM-сервисной информации для конкретного VIN.`
