# cad-fond-de-plan — urban morphology and metric CAD engine

Version 1.9.0. This repository implements deterministic footprint/block topology, a level-aware road graph, R2018 DXF output, and A3/A1/A0 paper-space layouts. Project coordinates remain projected metres; the Oran/western-Algeria profile uses UTM 30N / EPSG:32630 where appropriate.

See [`DEVELOPMENT_ARCHITECTURE_MAP.md`](DEVELOPMENT_ARCHITECTURE_MAP.md) for project history, layer schema, storage/quota guidance, and portability details. See [`SKILL.md`](SKILL.md) for operating rules and the source-data limitations.

## Deterministic developer commands

- `make test` — creates a temporary isolated environment, installs pinned project/test dependencies, runs the full test suite, smoke-imports the MCP server, then removes the environment.
- `make lint` — runs Ruff checks/format verification and Mypy in a temporary environment.
- `make build` — creates a wheel and source distribution in `dist/`.
- `make run-mcp` — launches the stdio MCP server with `uv`.

The full plan builder is network-enabled and writes to its requested output directory. Unit tests and lint do not run it. Do not call `generate_cad_plan` unless a plan generation is explicitly intended.

## Footprint providers

Microsoft Global ML remains the backward-compatible default (`--footprint-provider microsoft`). Google Open Buildings v3 is opt-in with `--footprint-provider google-open-buildings`; it accepts an offline `.csv`, `.csv.gz`, or directory of cached tiles via `--google-open-buildings-file`. Without a cache path, the provider discovers official v3 S2 tiles and downloads only intersecting tiles, with a 250 MB per-tile safety limit (`--google-max-download-mb` can raise it). Google scores are retained in `fond-de-plan-report.json`; the default minimum confidence is 0.65 and can be set with `--google-min-confidence`. OSM keeps priority during deduplication, and source IDs/confidence/license metadata are retained per output footprint.

### Region-aware confidence thresholds (v1.4.4)

Google publishes per-S2-cell score thresholds for **estimated** 80%, 85%, and 90% precision tiers. Pass
`--google-target-precision 80|85|90` to filter Google footprints against the threshold polygon that covers each
footprint centroid instead of one global cutoff. `--google-thresholds-path PATH` points at a local/cached copy of
`score_thresholds_s2_level_4.csv`; without it the official CSV is downloaded once and cached at
`<output>/source/google-open-buildings-score-thresholds.csv`.

Fallback never aborts a build: a centroid outside every published cell (`fallback-no-cell-match`), a cell with no
value for the requested tier (`fallback-cell-threshold-missing`), or an unusable/absent table
(`fallback-threshold-table-unavailable`) all revert to `--google-min-confidence` (default `0.65`). The default
`--google-target-precision None` keeps that baseline behavior unchanged.

`fond-de-plan-report.json` → `footprints` records `target_precision`, `precision_is_estimate`,
`precision_note` (stating that the tier is an estimate, not a guarantee), `thresholds_csv_path`,
`thresholds_resolution_source`, `threshold_resolution_counts`, `applied_confidence_thresholds`,
`resolved_s2_cell_ids`, and `source_feature_thresholds` (source ID, score, applied threshold, target tier, S2 cell
ID, resolution source). Each source footprint also carries `applied_confidence_threshold`, `target_precision`,
`s2_cell_id`, and `threshold_resolution_source` in its `attributes`.

Google Open Buildings is licensed under CC BY 4.0 or ODbL 1.0; preserve attribution from the generated report. The test suite uses a tiny checked-in CSV fixture and never downloads dataset tiles.

## Contracts (v1.5.0)

v1.5.0 adds no geometry. It makes the existing engine reachable and verifiable.

### One QA contract

`tools/deliverable_qa.py` is the single implementation of deliverable validation. The builder's
in-process QA, `tools/check.py`, and the MCP `verify_topology` tool all call it, so one artifact set
gets one verdict. Results carry `qa_contract_version`.

Layout expectations are declared rather than hard-coded: a run that emits only `A3 1:250` is judged
against `{A3-1_250}` (from its report) instead of failing for the absent A1/A0 sheets. With no
declaration the conservative A3+A1+A0 expectation still applies.

### Versioned report schema

`fond-de-plan-report.json` now carries `report_schema_version` (`1.0`), `engine_version`, an
ISO-8601 `generated_at_utc`, sha256 digests of every footprint source file, the score-thresholds
CSV, and any cached OSM input, plus the effective build parameters.

`--report-detail summary` omits the per-feature `output_provenance` and
`source_feature_thresholds` arrays (recording their counts instead) so archival reports stay
compact; `full` is the default and keeps them. MCP runs use `summary` and return the applied
confidence policy in the structured result.

`tools/report_schema.py` also exposes `is_deterministic(a, b)`, which compares two reports while
ignoring declared volatile fields such as `generated_at_utc`.

### MCP surface parity

`generate_cad_plan` now accepts `footprint_provider`, `footprint_file`, `osm_file`,
`study_radius_m`, `google_min_confidence`, `google_target_precision`, and
`google_thresholds_file`, and returns the resolved provider, confidence policy, applied thresholds,
and S2 cell IDs. Invalid combinations are rejected with an explicit message: a precision tier
requires the Google provider, and a tier without a cached thresholds CSV is refused so a tool call
never triggers a mid-run dataset download. `list_supported_formats` also reports providers and
tiers.

### CI

`.github/workflows/ci.yml` runs `make test` and then `make lint` on push and pull request.

## True print architecture (v1.9.0)

The sheet used to be a 420 mm **square** named "A3" in the report. The defect was not one number: the
square assumption was baked into six layers (`write_svg`, `mm_point`'s hardcoded 300 m half-extent,
`render_sheet`, `_svg_findings`, `_png_findings`, and the reported `scale`), while
`layout_engine.resolve_layout` — which already knew A3 is 420 × 297 and computed real ground extents —
only ever reached the DXF.

**One source of truth.** `layout_engine` owns ISO 216 geometry and everything that renders paper asks it.

| sheet | landscape | portrait |
|---|---|---|
| A3 | 420 × 297 mm | 297 × 420 mm |
| A1 | 841 × 594 mm | 594 × 841 mm |
| A0 | 1189 × 841 mm | 841 × 1189 mm |

Select orientation with `--orientation` (CLI) or `orientation` (MCP).

**Scale is declared, never fitted.** A page at 1:N draws ground at exactly `1000/N` mm per metre; content
is *positioned* inside the usable area and clipped to it. Nothing is rescaled to fit — that would be the
same defect in a new costume.

**The raster and the paper space are one page.** `SheetPlacement` reserves the cartouche band exactly as
the DXF paper space does, so both report the same ground extent for the same sheet, scale and orientation
(verified for all six sheet/orientation combinations).

**QA enforces aspect ratio.** `_svg_findings` compares both edges against the declared page and rejects a
square page for a rectangular sheet; `_png_findings` compares both axes; the builder additionally asserts
the raster's aspect ratio against the page.

**Honest reporting.** `report["scale"]` is the layout's denominator, `paper_layouts.sheet` describes the
page, and `paper_layouts.scale_consistent` says whether the page actually covers the requested radius.
A request that cannot fit is refused with a message naming the sheet and denominator, not silently
stretched. Layout-less runs keep the legacy square sheet, catalogued as `custom` rather than "A3".

## Authoritative invariant (v1.8.1)

A live trial against real Tlemcen data found that `--containment-policy absorb` deleted authoritative
OpenStreetMap buildings: 14 in, 11 out. Phase B runs over the merged set and applied the containment
policy to the authoritative records phase A had just protected, logging the loss as `absorbed` rather
than `authoritative_contained_dropped` — so the audit still reported `consistent: true`.

Authority is now an **invariant, not a phase**. A record present in the authoritative input set can
never be removed by any containment policy; when a policy would have removed it, the pair is recorded
with `decision: retained` and a stated reason, and the record is listed in
`authoritative_protected_from_absorption`. `absorb` still absorbs inferred footprints, so the policy
is not neutered — it simply cannot reach authoritative data.

Also in 1.8.1: `metadata_from_utm` accepts an absent ground area (a run without `--image` no longer
crashes with a bare `TypeError`), the builder and `tools/check.py` share one layout expectation, and
attribution/warnings only claim a user-supplied aerial when one was actually supplied.

## Workspace hygiene (v1.8.1)

`tools/workspace_hygiene.py` keeps the workspace small without ever risking evidence. Classification
is by **class, not age**, in three tiers:

| tier | contents | action |
|---|---|---|
| protected | `.git`, source trees, `tests/`, bundles, architecture maps, deliverables (`site-plan/`), user uploads, installer scripts | never removed |
| disposable | `__pycache__`, `.pytest_cache`, `.ruff_cache`, `.mypy_cache`, `build/`, `dist/`, `*.egg-info`, stray scratch virtualenvs | removed by `make clean` |
| retained | downloaded tiles and run outputs | kept unless `--include-data` **and** older than `--max-age-days` |

Protection is decided by the innermost path component, so `tools/__pycache__` is prunable while
`tools/` is not. Scratch space is standardised on `/var/tmp` (`TMP_ROOT ?= /var/tmp`), keeping
temporary operations off the bounded `/tmp` tmpfs, and `make build` depends on `make clean` so
packaging always starts from a clean tree.

## Containment policy (v1.8.0)

Similarity used to be scored as `intersection ÷ min(area)`, which is maximal both when one footprint
*contains* another and when two records describe the *same* building. An 8 m shop inside a 900 × 400 m
warehouse scored 1.0000; a genuine duplicate scored 0.9996. The two cases were indistinguishable, so
no threshold could separate them.

Reconciliation now asks three separate questions — IoU (same building?), containment (nested?), and
area ratio (scale disparity?) — and routes containment through a **declared policy**:

| `--containment-policy` | Behaviour |
|---|---|
| `nested` *(default)* | Keep both, record the parent-child relation and metrics |
| `absorb` | Drop the enclosed footprint (pre-v1.8.0 behaviour) |
| `absorb_when_coincident` | Drop only when outlines are largely coincident |

`nested` is the default because retention is non-destructive and reversible: a nested pair can be
absorbed later with one flag, while a silently deleted building cannot be recovered from the output.

Supporting flags: `--containment-threshold` (0.88), `--iou-duplicate` (0.80), and
`--min-child-area-m2` (0.0, protects small structures from absorption).

**Authoritative phase fixed.** An inferred footprint that merely *envelops* OSM records was
discarded wholesale, deleting buildings that exist. It is now retained and tagged
`contains_authoritative: ["osm:…"]`. An inferred footprint *inside* an authoritative one is still
superseded, and authoritative records are never dropped under any policy.

**Provenance.** `footprints.reconciliation` records the policy, counters, and one entry per
non-distinct pair — parent and child source IDs, providers, areas, class, direction, IoU,
containment, area ratio, the decision, the reason, and `dropped_source_id`. `audit.consistent`
must be `true`: it asserts every drop counted by deduplication has a matching reconciliation record.

Hierarchy roles (podium/courtyard/annex) are deliberately deferred until an authoritative height or
semantic-tag signal exists; inferring them from geometry would replace a geometry decision with a
silent semantic one.

## Scale-safe deduplication (v1.7.0)

`merge_footprint_records` previously compared each candidate only against the first 120 retained
records (`unique[:120]`). A duplicate of the 121st-or-later footprint was therefore kept silently —
reachable in dense fabric and on large A0 sheets, where 300 m of urban frontage can yield several
hundred footprints.

Duplicate lookup is now an incremental spatial hash whose cost tracks local density rather than the
number of retained records:

- records are bucketed into cells sized from the first footprint's extent;
- a candidate only materialises buckets overlapping its padded envelope;
- footprints larger than the grid (a warehouse among houses) go to an always-scanned overflow list
  so they can never be missed;
- results are identical to the exhaustive algorithm — verified by parity tests against a reference
  implementation across randomised inputs.

Measured on a synthetic grid: 4,096 footprints went from ~1.9 s (quadratic) to ~48 ms (near-linear).

`--dedup-window` is deliberately **not** exposed on the CLI: correctness is the default. Callers that
genuinely need a bounded scan can pass `dedup_window=N`, and the bound is then recorded in the report
rather than applied silently. `DeduplicationReport` counters (`candidates_compared`,
`candidates_skipped_by_index`, drop reasons, `bounded_window`) appear in
`fond-de-plan-report.json` → `footprints.deduplication`.

## Acquisition layer (v1.6.0)

`tools/acquisition.py` owns the mechanics that every footprint source needs: local-file and
directory handling, index and tile caching, atomic writes, per-object size limits, and post-download
integrity checks. Providers no longer touch `requests`, temporary files, or cache semantics.

Behaviour is governed by declared capabilities rather than by provider-name branching. Each source
publishes a `SourceCapabilities` record covering licence, default confidence, remote discovery,
local and directory caches, size limits, streaming, accepted suffixes, and whether it publishes
precision tiers. Unsupported operations fail with a message derived from that record, and
`provider_capabilities()` exposes the whole matrix to tooling and agents — it is echoed in the
report's `footprints.capabilities` and in MCP `list_supported_formats.source_capabilities`.

Divergences this removed: Microsoft previously buffered entire tiles in memory with no size cap and
no integrity check, while Google streamed with a cap. Both now stream, both are capped
(Google by `--google-max-download-mb`, Microsoft by the layer default), and both are gzip-verified
before entering the cache.
Partial downloads can no longer become visible: bytes land in a `.part` file and are renamed only
after every check passes.

Adding a source now means declaring capabilities and implementing `plan()`. `tests/test_acquisition.py`
demonstrates this with a `StubSource` that requires no changes to the layer.

## MCP server

The official Python MCP SDK's FastMCP server exposes `generate_cad_plan`, `verify_topology`, and `list_supported_formats`. `verify_topology` is read-only and restricts inspected DXFs to `CAD_FOND_DE_PLAN_ALLOWED_ROOT`. Generation creates a unique non-overwriting run directory beneath `CAD_FOND_DE_PLAN_OUTPUT_ROOT`.

For Claude Desktop, copy the `mcpServers.cad-fond-de-plan` entry from [`claude_desktop_config.json`](claude_desktop_config.json) and replace `/ABSOLUTE/PATH/TO/gravo-cad-engine` with the local repository path. The MCP process uses the standard stdio transport.

## Source/data hygiene

Raw GIS extracts, vector tiles, rasters, temporary environments, build output, and test caches are excluded by `.gitignore`. Keep only download recipes and lightweight provenance in source control. OSM and Microsoft data retain their original licenses; imagery-derived geometry is not a cadastral survey.
