---
name: cad-fond-de-plan
description: "Use when creating a metric monochrome Fond de Plan from OSM, Microsoft footprints, or a supplied reference image; export SVG/DXF/PDF/PNG and A3 zooms."
metadata:
  version: "1.4.2"
  tags: "geospatial, gis, cad, dxf, osm, urban-planning"
  triggers: "fond de plan, cadastral base map, building footprints to DXF, road casement, architectural site plan"
---

# Cad Fond De Plan

## Workflow
Build a clean vector base map in a local projected CRS. The build path resolves a map pin or coordinates, queries/caches OSM, uses Microsoft Global ML footprints by default or opt-in Google Open Buildings v3, normalizes geometries in UTM, regularizes footprints, conservatively snaps near-coextensive shared walls while preserving holes/area, nodes same-grade road centerlines into a graph, synthesizes node-aware curbs, reconciles near-curb parcel frontage, and traces the configured aerial profile when supplied. The R2018 DXF carries architectural G-* layers, reusable grid-cross/north-arrow blocks, and—when a site boundary is available—A3 1:250, A1 1:500, and A0 1:1000 paper layouts with UTM/WGS84/area cartouche and scale bar. Google Open Buildings is queried only when explicitly selected; preserve its CC BY 4.0 or ODbL 1.0 attribution and confidence scores.

## Inputs and defaults
- Required location: `--maps-url URL` or both `--lat LAT --lon LON`; UTM EPSG is selected from location.
- `--radius METRES` defaults to `300`; `--sheet-mm MM` defaults to `420` for the square base sheet.
- `--image PATH` enables the current manually traced profile, which requires its configured **1106×1280** pixel frame. `--anchor-x 493`, `--anchor-y 680`, `--gsd 0.12` are estimates, not survey control; replace them only with documented control.
- `--osm-file PATH` accepts cached OSM XML or GeoPackage. `--footprint-provider microsoft` is the default; select `google-open-buildings` to use Google Open Buildings v3. `--microsoft-file PATH` accepts a cached gzipped GeoJSONL tile. `--google-open-buildings-file PATH` accepts a cached Google CSV/CSV.GZ tile or directory; its default confidence threshold is 0.65 and automatic tile downloads are capped at 250 MB each. Omit the selected provider's file path to use its online acquisition path.
- `--output DIR` defaults to `~/site-plan`; `--json` emits the complete report.
- A3 zoom: `a3_layout_exporter.py --source SVG --output-dir DIR`; default source is `~/site-plan/fond-de-plan-hatched.svg`. It writes A3 landscape **420×297 mm at 1:250** and does not rebuild the base map.

## Usage
```bash
$HOME/.local/bin/cad-fond-de-plan --maps-url 'https://maps.google.com/…' --image /path/aerial.jpg --radius 300 --output ~/site-plan --json
# Or, from an Arena tool bridge:
python3 ~/tool-bridge.py run cad-fond-de-plan:build -- --help
# A3 derivative (requires the base hatched SVG):
$HOME/.local/bin/a3_layout_exporter --help
$HOME/.local/bin/cad-fond-de-plan-check ~/site-plan
```
Use direct `VENV/bin/python PATH/TO/tool.py` if launchers are not on `PATH`. Install a fresh environment with `bash INSTALL_IN_NEW_CHAT.sh /path/cad-fond-de-plan-skill.zip`.

## Rules
- Work in projected metres; preserve CRS/EPSG, source IDs, attribution, and uncertainty. Do not call aerial registration or ML footprints cadastral survey accuracy.
- Use vector footprints, not raster edge contours. Find the long/short MRR ratio correctly; estimate axial orientation from dominant local modes. Snap edges in that local frame, preserve concavities/holes, and keep area/aspect within 5%. If topology or correction bounds fail, retain and flag source geometry instead of replacing it with an MRR rectangle.
- Roads must retain graph connectivity, same-grade intersection nodes, bridge/tunnel separation, width-aware curb synthesis, and frontage reconciliation. Never join grade-separated crossings.
- Model-space geometry remains monochrome and metric. SVG base sheets stay unlabelled; DXF paper layouts may include vector cartouche, UTM grid labels, north arrow, and a true-distance scale bar. Hatch at 45° and clip to building rings/holes.
- Pen weights: walls/building outlines 0.50 mm; road casements 0.35 mm; hatching 0.13 mm; site boundary 0.60 mm dashed; parcels 0.18 mm.

## Prompt guidelines
Ask for the map link or coordinates, radius/extent, aerial image, any surveyed/GCP control and its pixel convention, desired page/scale, and existing OSM/Microsoft caches. If scale or control is unknown, state uncertainty and ask whether an estimate is acceptable. Confirm whether the requested deliverable is a new base map or a read-only layout derivative. Never infer legal parcel lines from imagery; distinguish traced site boundary from authoritative cadastral data.

## Tools
- `tools/cad-fond-de-plan-build.py`: base-map build and R2018 DXF architecture; see `--help` for parameters.
- `tools/urban_topology.py`: deterministic block, courtyard, road-graph, curb, and frontage helpers.
- `tools/layout_engine.py`: A3/A1/A0 scale/viewport resolver and vector paper-space annotations.
- `tools/cad-fond-de-plan-zoom-a3.py`: read-only A3 viewport exporter; `tools/a3_layout_exporter.py` is its portable CLI alias.
- `tools/check.py DIR`: deterministic package QA; `tools/cad-fond-de-plan-setup.sh`: isolated dependency setup.
- `tests/`: fast geometry, QA, and layout regression tests; run `pytest -q` from the skill root. Tests do not call the full render pipeline.

## Dependencies and provenance
Python 3.10+; see `requirements.txt`. OSM data is ODbL; Microsoft Global ML footprints are CDLA-Permissive-2.0. Preserve attribution and dataset snapshot/tile identifiers. Treat source images, OSM, and ML footprints as evidence with different accuracy, not interchangeable survey truth.

## Verification
Run `pytest -q` from the skill root and `python tools/check.py ~/site-plan`. Inspect the preview manually. Unit tests avoid the network and export pipeline; a passing test suite does not certify survey accuracy.

## Output
The base build writes hollow/hatched SVG, metric DXF, 300-DPI PNG/PDF, and a JSON provenance/QA report. The read-only A3 tool writes `fond-de-plan-zoom-a3.svg`, `.pdf`, and `-300dpi.png`. Never overwrite an existing plan without the user's explicit request.
