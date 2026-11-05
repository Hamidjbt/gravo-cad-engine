# Development Architecture Map — `cad-fond-de-plan`

**Current release:** 1.4.4  
**Last updated:** 2026-11-05  
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

- Skill/manifest version: **1.4.4**.
- Portable ZIP: `/home/user/cad-fond-de-plan-skill.zip` (46,418 bytes; ZIP integrity and required modules verified).
- Latest Drive backup: `cad-fond-de-plan-skill-1.4.2.zip`, ID `1G-5WKL9N6PpGndF5swgpa9RpL4x5bYVh`, 46,445 bytes.
- Latest full regression run: **51 passed**; Python compilation and JSON parsing passed.
- Validated A3 artifacts are protected and remain in `/home/user/site-plan/`.
- Full generation/render/export was intentionally not run during this engineering upgrade.
- Quota recovery left `/home/user` at **378 files / 43M** (target: <500 files / <100MB); filesystem use is 17%. The validated A3 SVG/PDF and all critical skill source remain. At the pre-cleanup inventory, only the A3 SVG and PDF were present; no A3 300-dpi PNG existed in `site-plan`, and none was removed.
