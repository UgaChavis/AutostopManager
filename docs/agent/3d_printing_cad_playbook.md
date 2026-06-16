# 3D Printing CAD And Anycubic Route

Use this route when the owner asks the manager to design a small printable
part, convert a photo or description into a CAD model, prepare STL, choose
material/orientation, or open the result for Anycubic Kobra S1 printing.

## Source Boundaries

- AutoStopManager stores the durable route and operating rules only.
- The CAD/STL source workspace is external to this repository and must be
  resolved from `AUTOSTOP_3D_WORKSPACE`. If that variable is unset or points to
  a missing directory, ask the owner for the current workspace before modeling.
- In the external workspace, OpenSCAD source files and `models/catalog.json`
  are the source of truth. Exported STL files under `exports/` are generated
  artifacts.
- Do not hand-edit STL files.
- Do not generate or store G-code in the repository unless the owner explicitly
  changes the v1 rule.

## First Read

After resolving `AUTOSTOP_3D_WORKSPACE`, read these files relative to that
workspace:

1. `AGENTS.md`
2. `README.md`
3. `docs/workflow.md`
4. `docs/mechanical-cad-playbook.md`
5. `docs/slicer-checklist.md`
6. `docs/remote-printing.md`
7. `docs/cad-stack-upgrade.md`
8. For print-process choices, query the local print knowledge base.

## Required Inputs

Capture these facts before modeling:

- purpose of the part
- critical outside dimensions
- mating hole, slot, clip, thread, or panel-stack dimensions
- fit target: clearance, sliding, snug, clamp, snap, press, or sacrificial
- expected load direction and abuse
- material and service environment: PLA prototype, PETG shop part, ASA/ABS/PA
  automotive or heat candidate, TPU soft interface
- desired print orientation and whether support is acceptable
- hardware used with the printed part
- quantity and whether the first print is a calibration coupon or usable part

If exact dimensions are missing, ask for the mating-part dimensions and desired
fit behavior. A photo is useful only when it includes a scale reference or a
measured mating feature.

## CAD Workflow

1. Choose the modeling tool:
   - OpenSCAD for simple parametric clips, spacers, brackets, bolts, nuts,
     bushings, gauges, and fast iteration.
   - BOSL2 for threads, rounded solids, masks, hinges, gears, clips, and reusable
     mechanical primitives.
   - build123d/CadQuery for BREP solids, STEP, assemblies, robust fillets, or
     drawing-friendly geometry.
   - FreeCAD TechDraw or SVG when a formal drawing or readable concept sketch is
     needed.
2. Use millimeters and named parameters only.
3. Register every printable model in `models/catalog.json`.
4. Build and validate before calling a model ready:

```powershell
.\.venv\Scripts\python.exe scripts\cad.py build <name>
.\.venv\Scripts\python.exe scripts\cad.py validate <name>
```

Use `calibration`, `templates`, `parts`, or `all` selectors when appropriate.

## Kobra S1 Baseline

- Printer: Anycubic Kobra S1 / Kobra S1 Combo class.
- Build volume: 250 x 250 x 250 mm.
- Nozzle baseline: 0.4 mm.
- Default first-pass material: PLA.
- Default first-pass layer height: 0.2 mm.
- Minimum functional wall: 1.2 mm; prefer 1.6-2.4 mm for loaded regions.
- Initial PLA diameter clearances: +0.25 mm snug, +0.35 mm normal sliding,
  +0.40 mm loose. Update only from real measured coupons.

## Print Rules

- Orient tensile and bending loads in XY filament paths where possible.
- Add walls before high infill: start with 4 walls, use 5-6 for clamps, shells,
  threads, and loaded small parts.
- Treat printed metric threads as prototypes. Prefer M6+ coarse threads; for
  M3-M5 prefer through holes, captive nuts, nut traps, or heat-set inserts.
- Do not copy injection-molded clips literally. Recreate the function with
  thicker beams, root radii, chamfers, pull tabs, and service grips.
- PLA is for fit tests and light indoor parts. For automotive heat, UV, repeated
  snap-fit, or constant load, move to PETG/ASA/ABS/PA after calibration.
- Dry PETG, PA/nylon, and TPU before functional prints.

## Slicing And Printer Handoff

Local v1 path:

1. Build and validate STL.
2. Open `exports/stl/<model>.stl` in Anycubic Slicer Next:

```powershell
.\.venv\Scripts\python.exe scripts\cad.py open-slicer <name>
```

3. In the slicer, confirm millimeter scale, bed position, build volume,
   material profile, supports, wall count, infill, brim/skirt, and seam position.
4. Send/print through the slicer, Anycubic app/cloud workflow, or USB, depending
   on the printer state. Keep generated G-code out of Git.

Automation option after explicit owner approval:

- Use `scripts/printer_connect.py` from `AUTOSTOP_3D_WORKSPACE` for safe local
  discovery, Moonraker status checks, and upload dry-runs.
- If Rinkhals is installed and Moonraker is reachable on the printer, use the
  Moonraker file upload API for generated `.gcode` files only after slicer
  output has been inspected and the printer is known safe to start.
- Default to dry-run. Real upload requires `--allow-upload`; starting a print
  also requires `--start-print` and explicit owner confirmation.
- Treat the official Anycubic Cloud/App route as a GUI workflow unless a public,
  documented local API is found. Do not reverse-engineer or reuse cloud tokens.
- Do not install custom firmware, change root/SSH/ADB access, start heating, or
  launch a print without explicit owner confirmation for that operation.
- Current local enhancement stack includes Blender, MeshLab, CloudCompare,
  FreeCAD, OpenSCAD/BOSL2, build123d/CadQuery, Anycubic Slicer Next, OrcaSlicer,
  Cura, Nmap, Tailscale, and local Rinkhals/Rinkhals.apps clones. Use
  `scripts/cad.py check` before relying on paths.

## Local Commands

```powershell
if (-not $env:AUTOSTOP_3D_WORKSPACE) { throw "Set AUTOSTOP_3D_WORKSPACE first." }
Set-Location -LiteralPath $env:AUTOSTOP_3D_WORKSPACE
.\.venv\Scripts\python.exe scripts\cad.py check
.\.venv\Scripts\python.exe scripts\cad.py list
.\.venv\Scripts\python.exe scripts\cad.py build calibration
.\.venv\Scripts\python.exe scripts\cad.py validate calibration
.\.venv\Scripts\python.exe scripts\print_knowledge.py search "printed threads PLA clearance"
.\.venv\Scripts\python.exe scripts\cad.py measurements summarize
.\.venv\Scripts\python.exe scripts\printer_connect.py inspect-local
.\.venv\Scripts\python.exe scripts\printer_connect.py scan --subnet 192.168.0.0/24
```

Record real printed dimensions in `measurements/pla-kobra-s1.csv` and use those
measurements to adjust tolerances.
