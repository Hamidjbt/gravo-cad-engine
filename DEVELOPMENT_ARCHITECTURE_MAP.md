# Development Architecture Map — `cad-fond-de-plan`

**Current release:** 1.9.0  
**Last updated:** 2026-11-06  
**Package source:** `/home/user/skills/custom/cad-fond-de-plan/`  
**Companion engineering log:** [`PRECISION_UPGRADE_AUDIT.md`](PRECISION_UPGRADE_AUDIT.md)

This manual records the project’s design history, architecture, engineering rules, portability path, and measured workspace-storage strategy. It is written to stand alone for a fresh development session.

---

## 1. Project vision and foundation

### 1.1 Scope and coordinate system

`cad-fond-de-plan` produces precise, monochrome vector **Fond de plan** base plans for architectural and urban-site work in Algeria. The regional design profile includes Oran and western Algeria in **WGS 84 / UTM zone 30N, EPSG:32630**, with projected metre coordinates retained throughout geometry operations and CAD model space.

The active builder chooses a UTM zone from the supplied coordinates; EPSG:32630 is not to be forced for locations outside its valid zone. The existing archived worked example in `site-plan/README.md` is for the Tlemcen area, not an Oran survey. Do not relabel that example as Oran or treat imagery-derived lines as surveyed cadastral truth.

The project combines OSM road/parcels context, Microsoft Global ML Building Footprints, and optionally a locally supplied reference aerial. Source provenance, attribution, and uncertainty must remain visible. The package does **not** currently implement Google Open Buildings ingestion; do not claim it was fetched.

### 1.2 Drawing and print standards

The package uses a monochrome CAD convention with project pen weights aligned to common ISO 128 line-weight practice:

| Feature | Project plot weight |
|---|---:|
| Building walls / outlines | 0.50 mm |
| Road curbs | 0.35 mm |
| Parcel lines | 0.18 mm |
| Building hatch | 0.13 mm |
| Site boundary | 0.60 mm, dashed |
| Shared-wall emphasis | 0.50 mm |
| Grid / detail annotation | 0.13–0.18 mm |
| Title-block border | 0.35 mm; internal details 0.18 mm |

The SVG base sheets remain monochrome and unlabelled for compatibility. DXF **paper space** may include the scale bar, north arrow, title block, and UTM labels. Metric model coordinates stay in metres; paper-space geometry is in millimetres. The R2018 DXF declares `$INSUNITS=6` and stores the selected EPSG in `$USERI1`.

A3 landscape historical zoom remains **420 × 297 mm at 1:250**. The multi-sheet DXF layout set is A3 1:250, A1 1:500, and A0 1:1000. The cartouche is a project-sized **180 × 50 mm DIN 6771-style vector title block**; this describes the chosen layout convention and is not a claim of third-party certification.

### 1.3 Non-negotiable geometry rules

- Keep projected metric coordinates; do not flatten curved/concave geometry into jagged raster edges.
- Preserve concavities and courtyard holes. Never prune a patio because it is narrow; report its width instead.
- Keep footprint area within ±5% for precision corrections. Preserve aspect ratio; never “fix” a long building by squeezing it into a fallback rectangle.
- Snap only supported, near-coextensive party edges; reject invalid or over-tolerance candidates and retain the original source.
- Do not connect grade-separated roads. Distinguish bridge/tunnel/layer metadata from at-grade graph nodes.
- Never overwrite or delete validated `fond-de-plan-zoom-a3.*` deliverables without explicit authorization.
- Do not run the full build/render/export pipeline as part of unit-test or code-review verification.

---

## 2. Chronological milestones and engineering evolution

### Phase 1 — Base vector extraction and A3 calibration

The initial workflow joined projected OSM roads/parcels and Microsoft footprint vectors with optional locally interpreted aerial traces. Geometry was kept in UTM metres and the legacy map sheet remained a 420 mm square base drawing.

A separate read-only zoom exporter derives a centered **A3 landscape 1:250** sheet from an existing SVG. It recalculates hatch pitch at the print scale, compensates stroke widths, and normalizes the crop viewport to zero origin. Its source SVG is opened read-only; it does not rebuild the base geometry. Validated outputs are named `fond-de-plan-zoom-a3.svg`, `fond-de-plan-zoom-a3.pdf`, and `fond-de-plan-zoom-a3-300dpi.png`.

### Phase 2 — Precision audit and defect discovery

The audit found that `rect_axis()` treated the minimum-rotated-rectangle’s four side lengths as though adjacent entries were unique dimensions. Opposite sides repeat, so `edges[0] / edges[1]` can compare equal long sides and report an elongated footprint as nearly square. It also reviewed inner courtyards, source/provider tile structures, road junction topology, and affine image-registration uncertainty.

A provider review did not find an executable Google Open Buildings ingestion routine. Microsoft quadkey and source-cache structures were documented separately from any unsupported provider claims. Geometry should remain evidence-based and provenance-aware.

### Phase 3 — Mathematical aspect-ratio correction

The MRR calculation now pairs the actual unique long and short dimensions: **`max(edge_lengths) / min(edge_lengths)`** (excluding degenerate edges). Axial orientation modes are treated as undirected and weighted by aspect/confidence. The constrained local-axis correction preserves area and target aspect; if topology, hole count, courtyard area, or limits fail, the original shape is retained rather than replaced by a rectangle.

The aspect-ratio expected failure was removed. The recorded phase-level result was **21/21 tests passing**; subsequent regression work expanded the active suite.

### Phase 4 — Urban-morphology and production CAD upgrade

Release **1.4.1** adds block-level shared-wall midpoint snapping, adjacency/face extraction, courtyard dimension diagnostics, a level-aware road-centerline graph, width-aware curb synthesis, and near-curb parcel-frontage reconciliation. R2018 DXF output now has a CTB-ready monochrome layer schema, reusable paper-space grid-cross/north-arrow blocks, and optional A3/A1/A0 layouts. The layout engine resolves the fixed 180 × 50 mm DIN-style vector cartouche, UTM/WGS84/area fields, grid annotations, and a true-distance scale bar.

The phase-4 regression suite uses synthetic geometry and a temporary DXF only. Final recorded result: **30 passed**. No production generation, rendering, or site-plan export was run for this upgrade. The validated historical A3 files were preserved.

### Phase 5 — Provider lineage and region-aware confidence filtering

Release **1.4.3** introduced the opt-in Google Open Buildings v3 provider (`--footprint-provider google-open-buildings`) with offline cached-tile support, score retention, and per-footprint provenance.

Release **1.4.4** adds region-aware confidence filtering. Google publishes per-S2-level-4-cell score thresholds for estimated 80%, 85%, and 90% precision tiers; the provider indexes those cell polygons with a Shapely `STRtree` and resolves each footprint centroid against them. `--google-target-precision [80|85|90]` (default `None`) selects the tier and `--google-thresholds-path` supplies a local/cached thresholds CSV, so resolution can run fully offline.

Fallback is explicit and never fatal: no covering cell, a cell missing the requested tier, or an unusable/absent table all revert to the baseline `0.65` minimum confidence with a distinct `threshold_resolution_source` label. Applied threshold, target tier, S2 cell ID, and resolution source are recorded on each source footprint and aggregated in the report (`footprints` section) along with a note that the tier is a dataset-published estimate rather than a measured accuracy. Offline fixtures and unit tests cover exact cell match, tier-dependent filtering, missing-value fallback, out-of-cell fallback, invalid precision, unreadable/missing threshold files, cached reuse, and report provenance.

### Phase 6 — Contract and surface parity (1.5.0)

Phase 5 added provider capability that the agent-facing MCP surface could not reach, and the two QA paths disagreed. Release **1.5.0** fixes both without adding geometry:

- **One QA contract.** `tools/deliverable_qa.py` implements deliverable validation once. The builder's in-process QA, `tools/check.py`, and MCP `verify_topology` all delegate to it, so one artifact set yields one verdict. Layout expectations became declarative: the report's `paper_layouts.sheets` defines what a run must contain, so a single-layout MCP run is no longer failed for absent A1/A0 sheets, while an undeclared run keeps the conservative A3+A1+A0 expectation. `check.py` shrank to a CLI shell; its stronger per-layer lineweight/color/linetype assertions became the shared baseline.
- **Versioned report schema.** `report_schema.py` owns `report_schema_version` (`1.0`), `engine_version`, `generated_at_utc`, sha256 digests for footprint sources, the thresholds CSV, and cached OSM input, plus effective build parameters. `--report-detail summary` replaces `output_provenance` and `source_feature_thresholds` with count-bearing omission records; `full` remains the default.
- **MCP surface parity.** `generate_cad_plan` gained `footprint_provider`, `footprint_file`, `osm_file`, `study_radius_m`, `google_min_confidence`, `google_target_precision`, and `google_thresholds_file`, and returns the resolved provider, confidence policy, applied thresholds, and S2 cell IDs. A precision tier requires the Google provider and a cached thresholds CSV, so a tool call cannot silently trigger a mid-run dataset download.
- **CI gate.** `.github/workflows/ci.yml` runs `make test` then `make lint` on push and pull request.

### Phase 7 — Shared acquisition layer (1.6.0)

By v1.5.0 the two providers each owned a private copy of acquisition mechanics, and the copies had already diverged: Microsoft buffered whole tiles in memory with no size ceiling and no integrity check, while Google streamed with a `max_download_mb` cap and atomic writes. A third source would have produced a third variant.

Release **1.6.0** introduces `tools/acquisition.py`:

- **`SourceCapabilities`** — a declared, serializable record per source (licence, default confidence, remote discovery, local/directory cache, size limits, streaming, accepted suffixes, precision-tier support). `require()` turns a missing capability into an explicit `CapabilityError` instead of silent behaviour.
- **`AcquisitionLayer`** — one implementation of local resolution, cache probing, atomic `.part` → final rename, streaming and buffered transfers, byte ceilings checked against declared size, `Content-Length`, and the running count, plus `gzip`/`sha256`/`none` integrity modes. Statistics: `DownloadLimitExceeded`, `IntegrityError`, `AcquisitionError`.
- **`TilePlan` / `RemoteTile`** — providers describe *what* to fetch; the layer decides *how*.
- **`AcquisitionSource`** — the extension point. A new provider implements `plan()` only.

`MicrosoftFootprintProvider` and `GoogleOpenBuildingsProvider` were rewritten onto this layer; both lost their private download code and their `requests`/`tempfile` imports. `provider_capabilities()` publishes the matrix, which now appears in the report (`footprints.capabilities`) and in MCP `list_supported_formats.source_capabilities`.

A test-only `StubSource` proves the extension point: a third provider needs no edits to the layer.

### Phase 8 — Scale-safe deduplication (1.7.0)

`merge_footprint_records` bounded duplicate detection with `unique[:120]`. Beyond 120 retained
footprints a duplicate was kept silently, which dense fabric and large A0 sheets can reach.

Release **1.7.0** replaces the fixed window with an incremental spatial hash:

- **Bucketing** — cells sized from the first footprint's extent; a candidate materialises only the
  buckets overlapping its padded envelope, so comparisons scale with local density.
- **Overflow list** — records larger than the grid (a warehouse among houses) are always scanned,
  so oversized footprints can never be missed.
- **Constant-time insertion** — an `STRtree` rebuild per insert had made the merge quadratic
  (~1.9 s for 4,096 records); the hash brought that to ~48 ms.
- **Explicit bounds** — `dedup_window=N` still bounds the scan for callers that want it, and the
  bound is reported rather than silent. `0` or negative means unbounded.
- **Diagnostics** — `DeduplicationReport` records drop reasons, comparisons, index skips, and any
  applied bound; it surfaces as `footprints.deduplication` in the build report.

The merge is not merely faster or wider: parity tests assert identical output to the exhaustive
reference algorithm across grid, randomised, mixed-size, and oversized-footprint inputs. A
performance guard fails the suite if comparison counts regress toward exhaustive scanning.

### Phase 9 — Explicit containment policy (1.8.0)

The v1.7.0 milestone left one documented semantic question: a large footprint absorbed small ones it
covered. Investigation showed the cause was not the threshold but the metric. `intersection ÷ min(area)`
is maximal for containment *and* duplication alike (1.0000 versus 0.9996 on representative geometry),
so no threshold value could distinguish them.

Release **1.8.0** introduces `tools/reconciliation.py`:

- **`OverlapMetrics`** — intersection, union, containment, IoU, and area ratio. IoU is what separates
  the two phenomena, by three orders of magnitude.
- **`ContainmentPolicy`** — `nested` (default, retain and report), `absorb` (historical), or
  `absorb_when_coincident`. `nested` is the default because retention is reversible and deletion is not.
- **`decide()`** — one place where classification becomes an action, with a human-readable reason.
- **`ReconciliationLog` / `ReconciliationRecord`** — an auditable entry per non-distinct pair, ordered
  deterministically so reports are reproducible.

Both merge phases now use the classifier. Phase A no longer discards an inferred footprint that
envelops authoritative records: it is retained and tagged `contains_authoritative`. Phase B routes
containment through the policy instead of absorbing silently.

Two earlier justifications were corrected during implementation: the merge was already
order-independent (so no ordering fix was needed), and the authority phase was not an inversion but a
mirror defect that deleted the *enveloping* record.

`--containment-policy`, `--containment-threshold`, `--iou-duplicate`, and `--min-child-area-m2` are
exposed on the CLI; MCP `generate_cad_plan` forwards `containment_policy` and returns
`nested_pairs_reported`. `footprints.reconciliation.audit.consistent` asserts that every drop counted
by deduplication carries a reconciliation record.

### Phase 10 — Live trial, authoritative invariant, storage hygiene (1.8.1)

v1.8.0 was validated end-to-end for the first time against live providers at Tlemcen
(34.884528, −1.301250, R = 300 m): 274 Microsoft footprints + 14 OSM buildings → 286 output
footprints in 39.6 s. The geometry, deduplication and reconciliation paths behaved as designed —
three nestings retained and reported, zero absorbed, `audit.consistent: true`.

The trial also found four defects that unit tests could not have caught, because none of them had
ever been exercised:

- **D1 data loss.** `absorb` deleted authoritative OSM buildings (14 → 11). Phase B applies the
  containment policy to the merged set, which includes the authoritative records phase A protects.
  The loss was logged as `absorbed`, so the audit stayed clean — a false negative in the very
  mechanism built to prevent silent deletion. **Fixed** by making authority an invariant: no policy
  may remove an authoritative record, and attempts are recorded in
  `authoritative_protected_from_absorption`.
- **D2 crash.** A run without `--image` has no site polygon, so `metadata_from_utm` called
  `float(None)` on the ground area and aborted the build after acquisition. **Fixed**; the cartouche
  already had a "NOT SPECIFIED" branch.
- **D3 divergent verdicts.** The builder passed `expected_layouts=frozenset(())`, which made the
  DXF layout assertion vacuously true, while `tools/check.py` demanded the legacy A3/A1/A0 set on
  the same directory. One artifact set must not have two verdicts. **Fixed**: a report declaring no
  layouts declares an expectation of none, and both callers apply it.
- **D4 false provenance.** The report claimed a "manual vector trace from supplied aerial" and
  attributed a user-supplied aerial when none was given, plus warnings for a JPEG that did not exist.
  **Fixed**: every provenance clause and warning is now conditional on the event it describes.

Phase 10 also adds `tools/workspace_hygiene.py` and a three-tier storage protocol (protected /
disposable / retained), with `make clean`, `make hygiene-report` and `make hygiene-check`. Scratch
space moves to `/var/tmp`. The governing rule is that the only things ever deleted are those that
can be regenerated.

### Phase 11 — True print architecture (1.9.0)

Every release through 1.8.1 printed a square sheet. The raster artists and the PDF were produced by
`sheet_mm / (2 * radius)` scaling on a 420 mm square page, while the DXF carried a correct ISO paper
space nobody else consulted. Two artifacts in one deliverable described different pages.

`layout_engine` now owns the geometry:

- **`page_size(sheet, orientation)`** returns exact ISO 216 edges; the table stores long/short edges so
  "A3" can never silently mean "420 square".
- **`SheetPlacement`** is the metre-to-millimetre map for one printed page. It reserves the cartouche
  band exactly as `resolve_layout` does, so the raster and the DXF paper space agree on ground extent
  for every sheet/orientation pair.
- **`resolve_placement`** centres content at a declared denominator and never rescales.
- **`coverage_bounds`** distinguishes "the radius bounds the page" from "the page bounds the radius",
  which is what `scale_consistent` reports.

The raster pipeline now derives its pixel dimensions per axis from the page, clips the map to the usable
area (a `clipPath` whose absence would silently unclip the sheet), and refuses to emit a square raster for
a rectangular page. MCP gained an `orientation` parameter, and the DXF writer received the orientation the
report declares — without it a portrait run produced a portrait report next to a landscape layout tab.

Validated on the live Tlemcen case: A0 1:1000 in both orientations, and a documented refusal for A3 1:500
and A1 1:1000, which cannot hold a 600 m site. Measured output: 14043 × 9933 px at 300 dpi = 1189 × 841 mm
exactly, at a uniform 11.811 mm scaling.

### Phase 12 — Artifact egress pipeline (1.10.0)

Rendering a true A0 sheet at 300 dpi produces roughly 13 MB of raster alone. In a workspace with a hard
persistent-quota ceiling, "render it, then delete it" is a workaround that quietly caps the engine: the
larger the sheet, the more likely the write itself fails. `tools/egress_manager.py` removes artifact
*retention* from the monitored tree instead of removing resolution from the renderer.

- **`ArtifactEgress`** is a context manager that owns a private buffer under the system scratch
  directory (`/var/tmp/gravo_egress_buffer/<run_id>/` where `/var/tmp` is writable, else the system
  temp directory). It refuses to construct if the buffer root resolves inside the monitored tree — the
  tree named by `GRAVO_WORKSPACE`, else the user's home directory — so the invariant cannot be broken
  by configuration.
- **Registration is path-checked.** `register()` rejects any artifact that does not resolve inside the
  buffer, so a stray write elsewhere is an error rather than a silent leak.
- **Flush verifies bytes.** Each artifact is streamed to a sink, and the remote byte count must equal the
  local one; a mismatch fails the run rather than recording a false success.
- **Purge is unconditional.** `__exit__` removes the buffer on the success path *and* the exception path,
  then verifies it is gone.
- **The receipt is the only durable trace**: `home/<run_id>_receipt.json` holds remote URIs, per-file
  chunk counts, SHA256 checksums, a self-checksum, and the measured workspace delta. It is written `0444`
  and refuses to overwrite an existing receipt.

Sinks are pluggable: `GoogleDriveSink` (googleapiclient, `MediaFileUpload` in resumable mode),
`ResumableSessionSink` (streams over pre-authorized Drive session URIs with plain HTTP so a
credential-less environment can still egress), `LocalDirectorySink` (CI/offline verification), and
`NullSink`.

One service constraint was learned the hard way and is now encoded rather than assumed: a multi-chunk
resumable session **must** declare its total size when created. Attempting an undeclared 900 kB upload
was rejected with HTTP 400, *"not a multiple of the 262144 byte chunk granularity"*. Consequences:

- `ResumableSessionSink` requires a declared total for any artifact larger than one chunk, and refuses a
  declared total that contradicts the artifact's real size.
- The report embeds its own egress block and therefore grows while describing itself, which made its
  final size unpredictable. It is now padded to a fixed `REPORT_BLOCK_BYTES` (256 KiB) block: trailing
  JSON whitespace is insignificant, and the announced and delivered sizes then agree by construction.

Validated live on the Tlemcen case: A0 1:1000, radius 300 m, 404 buildings, `ok: true`, `findings: []`.
Six artifacts totalling **15,444,614 bytes** streamed to `Gravo_CAD_Deliverables/v1.10.0/` — the 12.7 MB
raster alone took 49 chunks — with `workspace delta: 0 bytes` and the buffer purged on exit.


### Phase 13 — DXF plotting standards (1.11.0)

Geometrically correct DXF is not the same as a plottable one. This phase adds the
styling layer a large-format plotter needs, and in doing so uncovered three defects
that only a plot would have revealed.

**Layers, colours, lineweights.** `tools/dxf_standards.py` is now the single table of
layer names, ACI colours and lineweights, imported by both the builder and the QA gate,
so the two cannot disagree. Structural ink stays on adaptive ACI 7 (black on white,
white on black); context takes real indices (8 grey, 9 light, 1 primary) so a CTB pen
table can key off them.

**The defect that started it.** `LayerTable.add(dxfattribs={"color": …, "lineweight": …})`
silently discards *both* values in ezdxf 1.4.5, leaving every layer at ACI 7 with
lineweight BYLAYER. Every DXF shipped from v1.9.0 through v1.10.0 was therefore uniformly
unstyled — the intent was in the source and in the file's memory, never on disk. Layers
are now created with explicit assignment (`create_layer`), and a regression test pins the
library behaviour so the trap is visible if it ever changes.

**Architectural hatching.** One HATCH per building, `SOLID` poché by default with
`--hatch-pattern ansi31` opt-in. Courtyards are inner boundary paths with `flags=0`;
outer rings use `flags=1`. `hatch_style` is written explicitly as 0 (nested/odd-parity)
instead of relying on the library default, because that group code — not the path flags
— is what determines island behaviour in a plotting application.

**The second defect.** The pattern scale was derived from ANSI31's *offset magnitude*
(3.175) instead of its *perpendicular line spacing* (3.175 × cos45° = 2.2451), making
every pattern 41 % too coarse; and an earlier draft used millimetres for a
drawing-unit quantity, a 25.4× error. Both are invisible in the DXF and obvious on paper.
`pattern_scale_for(denominator)` now derives the scale so a 2 mm pitch lands on paper at
any plot scale: 0.223 at 1:250, 0.445 at 1:500, 0.891 at 1:1000.

**Clean road polygons.** The road casement is already a polygonized corridor, so its
polygons are emitted as closed `G-ROAD-SURFACE` polylines. When polygonization yields
nothing, the run falls back to open curb boundaries and the report states
`road_geometry.mode = "open-boundaries-fallback"` with a reason — it never claims a
closure it does not have.

**The third defect: non-deterministic bytes.** DXF writers stamp wall-clock time
(`$TDCREATE`, `$TDUPDATE`, …) and fresh GUIDs into every file, so identical inputs
produced different bytes and different sizes. That is merely untidy for reproducibility
but fatal for a size-declared resumable upload. `save_dxf_deterministic` canonicalises
four header timestamps, the ezdxf version stamp and all GUIDs; two runs now produce
byte-identical files.

**Renderer limitation, recorded so nobody rediscovers it.** ezdxf's own drawing add-on
renders SOLID hatches correctly but draws *nothing* for pattern hatches — ANSI31
produced zero non-white pixels across four scale and size combinations while the DXF
tags were valid (`2:ANSI31`, `70:0`, `75:1`, `41:0.891`). Verification therefore runs on
three axes: DXF group codes, independent point-in-polygon geometry, and a budget ceiling.
Visual ANSI31 sign-off requires real CAD or a plotter.

---

## 3. System architecture and modules

### 3.1 Data flow

```text
OSM / Microsoft footprints / optional aerial
        │
        ▼
CRS selection → projected metres (UTM) → source filtering/deduplication
        │
        ├── building axis analysis → conservative orthogonalization
        │                         → shared-wall block topology / courtyard diagnostics
        ├── road centerlines → same-level graph → curb corridor → frontage reconciliation
        └── site geometry → focus bounds / area / coordinate metadata
                                      │
                                      ▼
                    SVG base sheets + metric R2018 DXF
                                      │
                         optional A3/A1/A0 paper tabs
```

### 3.2 Core modules

| File | Responsibility |
|---|---|
| `tools/cad-fond-de-plan-build.py` | Active acquisition/build/export entrypoint. Selects UTM, processes footprints, calls urban topology and layout helpers, writes legacy vector outputs and R2018 DXF. Do not invoke `build()` for code-only validation. |
| `tools/urban_topology.py` | Geometry-only deterministic functions: shared-edge candidate matching and midpoint correction, area/topology guards, courtyard dimensions, adjacency/faces, same-level road graph, curb corridor, frontage reconciliation. No network access or output writes. |
| `tools/layout_engine.py` | A3/A1/A0 landscape calculations, scale fit checks, UTM→WGS84 metadata, SVG/DXF cartouche, scale bar, paper-space viewport, grid labels/crosses, reusable north arrow. Model metres and paper millimetres are kept distinct. |
| `tests/test_urban_engine.py` | Nine synthetic regression tests covering shared walls/areas/holes, courtyard thresholds, road nodes/grade separation, frontage snapping, scale fit, metadata units, and temporary R2018 DXF layouts. |
| `tools/cad-fond-de-plan-zoom-a3.py` | Read-only historical A3 crop exporter; does not regenerate source model geometry. |
| `tools/check.py` | Package checker for SVG/PDF/PNG/DXF/report. Allows intended annotations only in DXF paper space and validates R2018 layer/block/layout architecture. |
| `tools/cad-fond-de-plan-setup.sh` | Creates an isolated Python environment and installs `requirements.txt`; see quota-aware path below. |

### 3.3 DXF architectural layer assignments

All new `G-*` layers use monochrome ACI color 7. Lineweights in the DXF layer table are hundredths of a millimetre; selected entities in the title block may override the layer default.

| Layer | Purpose | Weight | Linetype |
|---|---|---:|---|
| `G-BLDG-WALL` | Building exterior and courtyard rings | 0.50 mm | Continuous |
| `G-BLDG-SHARED` | Shared-wall emphasis | 0.50 mm | Continuous |
| `G-ROAD-CURB` | Synthesized road corridor boundaries | 0.35 mm | Continuous |
| `G-ROAD-CL` | Noded road centerlines | 0.18 mm | `CENTER` |
| `G-PARCEL` | Parcel/frontage lines | 0.18 mm | Continuous |
| `G-HATCH` | Clipped 45° building hatch | 0.13 mm | Continuous |
| `G-SITE-BOUNDARY` | Site perimeter | 0.60 mm | `SITE_DASH` |
| `G-ANNO-GRID` | UTM ticks, labels, and grid-cross inserts | 0.13 mm |
| `G-ANNO-NORTH` | North-arrow block and label | 0.25 mm |
| `G-ANNO-TTLB` | Cartouche border/details/text and scale bar | 0.35 mm layer; details 0.18 mm |

Legacy `BUILDINGS`, `HATCH`, `ROADS`, `PARCELS`, and `SITE_BOUNDARY` layer names are retained for downstream compatibility. Model-space geometry remains metric; paper layouts carry display-size annotations and the fixed 180 × 50 mm title block.

### 3.4 Verification posture

Run synthetic unit tests and static/package checks only when the task disallows generation. The final upgrade used the isolated environment’s `python -m pytest -q` (**30 passed**), Python compilation, and JSON validation. The temporary DXF smoke fixture was created under pytest’s temporary directory. These checks do not certify cadastral accuracy, survey control, licensing, or a generated site plan.

---

## 4. Portability and reuse in fresh sessions

### 4.1 Obtain and install the portable bundle

The current portable archive is `/home/user/cad-fond-de-plan-skill.zip`; its root contains `cad-fond-de-plan/`. The same release is backed up in Google Drive `AI_Skills` as `cad-fond-de-plan-skill-1.4.2.zip` (46,445 bytes; Drive ID `1G-5WKL9N6PpGndF5swgpa9RpL4x5bYVh`). Download it in a new session, then run the bundle’s safety-checking installer:

```bash
bash /path/to/INSTALL_IN_NEW_CHAT.sh /path/to/cad-fond-de-plan-skill.zip
```

Check the archive before installation with `unzip -t /path/to/cad-fond-de-plan-skill.zip`. The installer validates ZIP paths and the manifest before installing dependencies, stages extraction, backs up an existing skill directory instead of silently replacing it, and creates local command launchers. Do not point it at a site-plan deliverable directory. For a deliberate manual unpack into an empty custom skill root, use `mkdir -p "$HOME/skills/custom" && unzip -q /path/to/cad-fond-de-plan-skill.zip -d "$HOME/skills/custom"`; this creates `$HOME/skills/custom/cad-fond-de-plan/`. Then run that directory's `tools/cad-fond-de-plan-setup.sh` and use the platform-specific registration notes below.

### 4.2 Claude Code

The installer auto-selects `$HOME/.claude/skills` when `$HOME/.claude` exists. To be explicit:

```bash
CAD_SKILLS_ROOT="$HOME/.claude/skills" \
  bash /path/to/INSTALL_IN_NEW_CHAT.sh /path/to/cad-fond-de-plan-skill.zip
```

Then start a fresh Claude Code session and refer to the installed `cad-fond-de-plan/SKILL.md`. The tools are ordinary Python files; use the provided launcher or the isolated environment’s Python. Do not trigger the full builder unless a user explicitly requests generation.

### 4.3 Arena

For an Arena workspace using `~/skills/custom`:

```bash
CAD_SKILLS_ROOT="$HOME/skills/custom" \
  bash /path/to/INSTALL_IN_NEW_CHAT.sh /path/to/cad-fond-de-plan-skill.zip
```

If Arena’s `skill-manager.py` and `tool-bridge.py` helpers exist, the installer validates/indexes the package. Otherwise, check `skill.json` and the listed tool paths manually. Arena skill registration depends on its platform bridge; filesystem installation alone is not a universal registration API.

### 4.4 ChatGPT

There is no platform-independent filesystem command that registers a custom ChatGPT skill. Upload/import the ZIP or `SKILL.md` using the controls available in that ChatGPT workspace, or attach the bundle as project knowledge. To execute the Python tools, expose them through that session’s supported code/filesystem tool. Do not claim the generic installer has registered the skill inside ChatGPT.

### 4.5 Direct CLI use

After installation, the documented launchers are:

```bash
$HOME/.local/bin/cad-fond-de-plan --help
$HOME/.local/bin/a3_layout_exporter --help
$HOME/.local/bin/cad-fond-de-plan-check /path/to/output
```

The A3 tool is read-only with respect to its source SVG. The package requirements are in `requirements.txt`; keep any downloaded source data in a deliberate cache, not mixed with core code.

---

## 5. Session quota: measured environment and native storage strategy

### 5.1 What was actually mounted

Inspection on 2026-11-04 found:

| Path / resource | Observed mount or size | Persistence/quota implication |
|---|---|---|
| `/home/user` | Root ext4 `/dev/vda` (`/dev/root`), 25G total, 4.8G used, 20G free, 20% | Session workspace tree. Snapshot quota is a separate limit (about 128 MB / 10,000 files); `df` free space does not mean every file counts toward the snapshot. |
| `/tmp` | tmpfs, 992M total; 59M used at inspection | Fast, outside `/home/user`, but ephemeral and memory-backed. Good for temporary tests/download scratch, not durable source or deliverables. |
| `/dev/shm` | tmpfs, 992M total, empty at inspection | Ephemeral shared-memory scratch only; not a persistent dependency store. |
| `/run` | tmpfs, 397M total | System runtime only; not a workspace mount. |
| `/mnt`, `/workspace` | No separate persistent/unmetered mount was visible in `df`/`mount` output | Do not assume such a mount exists without rechecking in a future session. |

At inspection, `du -sh /home/user` was **774M**, and `find /home/user -type f | wc -l` was **15,241**. Dominant sources were the newly installed `~/.venvs/cad-fond-de-plan` environment (**578M**) and pip’s `~/.cache/pip` (**135M**); source extracts were a few megabytes. That `~/.venvs` spelling is important: the platform’s excluded directory name is `.venv` (singular), not `.venvs`, so the former can still inflate snapshot file count. Root disk usage was only 20%; the main issue was the session tree’s counted files/bytes, not a full filesystem. After the main purge, a separate scan identified a 928-file Node V8 compile cache at `~/.hf-work/tmp/node-compile-cache/` (about 7.7 MB). It is disposable compiler cache, not user source; it was documented here before its removal to meet the stricter 500-file target. Other `motion-projects`/QA/render files were left untouched as user content.

### 5.2 Environment-native ways to stay under quota

1. **Use the exact platform-excluded cache/environment directories.** The workspace snapshot excludes directories named `.cache`, `.venv`, and `__pycache__` (among others). Put pip’s cache at `$HOME/.cache/pip` and the project environment under `$HOME/.venv/cad-fond-de-plan` (singular `.venv`). Update `CAD_FOND_DE_PLAN_VENV` to that path when needed. Do not use `$HOME/.venvs` for snapshot-sensitive environments. These paths remain on the same root filesystem, but are excluded from snapshots by directory-name policy; this is different from an unmetered mount.
2. **Do not symlink a persistent runtime environment into the workspace.** Symlink targets under `/tmp` or `/dev/shm` disappear on restart and are not captured; links can become broken. Keep durable source and requirements in the workspace, use the platform-excluded `.venv`/`.cache` paths for installed packages and caches, and accept that excluded state may need hydration/recreation after a fresh container if caches are not retained by that session.
3. **Treat `/tmp` and `/dev/shm` as scratch, not persistence.** Download temporary rasters, expanded vector tiles, and synthetic test data there, process them, and copy only the small final evidence files into the workspace. Their capacity is finite and memory-backed.
4. **Use Google Drive for durable push/pull, not as a mounted Python filesystem.** Keep versioned source ZIPs, audit/manuals, and deliberately selected final deliverables in `AI_Skills` / `site-plan` Drive folders. Pull an archive into a fresh session, verify its checksum/ZIP, work in the source tree, then push a new versioned archive after tests. Do not attempt to load native Python modules directly from Drive; hydrate/download first. Cloud-backed runtime requires the Drive connector or a configured Drive API, not an assumed local mount.
5. **Prefer a wheelhouse archive only when repeat installs justify it.** On the same Linux/Python/ABI target, build wheels with `pip download -r requirements.txt -d /tmp/cad-wheelhouse`, archive the wheelhouse to one `.tar.zst`/`.zip` file, and store it in Drive. Hydrate into `/tmp`, then install with `pip install --no-index --find-links=/tmp/cad-wheelhouse -r requirements.txt`. A compressed wheel bundle reduces file-count pressure while archived, but is not a portable virtualenv and may not work across Python versions, CPU architectures, or OS/libc differences. Requirements plus lock/pinned versions are safer than copying `site-packages`.
6. **Keep GIS intermediate sources reproducible.** OSM extracts and Microsoft tiles can be reacquired from the URLs/quadkey recipe recorded in source code/report. Store the download recipe and provenance, not every raw cache file. Preserve any source only when offline reproducibility or a license/retention requirement makes it necessary.

### 5.3 Recommended default for future sessions

- Durable, small: `/home/user/skills/custom/cad-fond-de-plan/` (code, tests, docs, requirements) and `/home/user/site-plan/` (validated outputs, audits, recipes).
- Runtime: `$HOME/.venv/cad-fond-de-plan/` (singular excluded directory); pip cache `$HOME/.cache/pip/`.
- Scratch: `/tmp/cad-fond-de-plan-*` for temporary raw GIS tiles, build fixtures, and test artifacts; delete when finished. Use `/dev/shm` only for small, short-lived memory work.
- Backup: versioned source bundles in Google Drive; verify package hash/size after round-trip. Keep final plan deliverables separately and never purge them as cache.
- Quota check before/after maintenance: `find /home/user -type f | wc -l`, `du -sh /home/user`, and `df -h /home/user`. Count and size checks answer different questions. If workspace exceeds the active user threshold, prune only known disposable caches/intermediates and recheck.

---

## 6. Current package and validation record

- Skill/manifest version: **1.11.0**.
- Portable ZIP: `/home/user/cad-fond-de-plan-skill.zip` (46,418 bytes; ZIP integrity and required modules verified).
- Latest Drive backup: `cad-fond-de-plan-skill-1.4.2.zip`, ID `1G-5WKL9N6PpGndF5swgpa9RpL4x5bYVh`, 46,445 bytes.
- Latest full regression run: **334 passed**; `make lint` clean on 29 files; mypy clean.
- Validated A3 artifacts are protected and remain in `/home/user/site-plan/`.
- Full generation/render/export was intentionally not run during this engineering upgrade.
- Quota recovery left `/home/user` at **378 files / 43M** (target: <500 files / <100MB); filesystem use is 17%. The validated A3 SVG/PDF and all critical skill source remain. At the pre-cleanup inventory, only the A3 SVG and PDF were present; no A3 300-dpi PNG existed in `site-plan`, and none was removed.
