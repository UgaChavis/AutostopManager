# Fluid Maintenance Playbook

Use this playbook when the owner asks for engine oil, transmission fluid,
gear oil, ATF/CVT/DCT fluid, transfer-case fluid, differential oil, axle oil,
Haldex/AWD coupling fluid, brake fluid, coolant, power steering fluid,
maintenance interval, or any fill capacity.

## Source Order

1. Identify the exact vehicle: VIN or chassis/frame number, market, year, make,
   model, engine code, transmission type/code, drivetrain, body/grade.
2. Identify the exact unit: engine, AT, MT, CVT, DCT, transfer case, front/rear
   differential, center differential, Haldex/AWD coupling, power steering,
   brake system, coolant, or other reservoir.
3. Use OEM owner manual or OEM service data for the exact market first.
4. If OEM data is unavailable, use licensed professional databases such as
   MOTOR TruSpeed Repair, Autodata, Mitchell ProDemand, TecAlliance, HaynesPro,
   ALLDATA, or Bosch ESI[tronic].
5. Use lubricant selectors only as cross-check and product mapping after the
   OEM specification is known.
6. If source-backed data is unavailable, say:
   `Требуется проверка по OEM-сервисной информации для конкретного VIN.`

## Required Distinctions

Do not collapse these values into one number:

- oil-only change vs oil-and-filter change
- drain-and-refill vs dry fill/overhaul
- pan removal vs simple drain
- cooler/line drain vs normal service
- level-check temperature and procedure
- FWD/RWD/AWD drivetrain differences
- engine code and transmission code differences
- market/region differences

## Output Contract

Every fluid answer must include:

- vehicle identity used
- unit and service operation
- required fluid spec/approval, not only viscosity
- approximate capacity and whether it is refill/dry/with filter
- source name and URL/license status
- uncertainty and next check when data is not confirmed

## Product Selector Role

Castrol, LIQUI MOLY, Mobil, AMSOIL, Motul, RAVENOL, Shell, and Valvoline
selectors can help map OEM specifications to available products. Treat their
results as supplemental unless they are explicitly quoting OEM data.

Mercedes-Benz Operating Fluids and ZF lubricant references are useful official
starting points for approval families and product mapping. They do not, by
themselves, confirm the exact vehicle, unit variant, service fill amount, or
level-check procedure. Use the ZF/Mercedes reference route only together with
the vehicle and aggregate context required above.

## Tooling

Use raw capability discovery, schema lookup, and `call_raw_capability` for
`recommend_automotive_sources` with `data_type="fluids"`. The compatibility
name `recommend_fluid_maintenance_sources` is a direct alias and does not set
`data_type` automatically.
