"""Typed MCP façade for generation and read-only DXF topology inspection.

The tool signature mirrors the builder's supported input surface: provider
selection, cached source files, study radius, area selection, and Google
Open Buildings precision thresholds. Unsupported combinations are rejected here
with an explicit message rather than silently ignored by the subprocess.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import uuid
from enum import Enum
from importlib.resources import files
from pathlib import Path
from typing import Annotated

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field

mcp = FastMCP("cad-fond-de-plan")
SCALES = {"1:250": 250, "1:500": 500, "1:1000": 1000}
SHEETS = {"A3": (420.0, 297.0), "A1": (841.0, 594.0), "A0": (1189.0, 841.0)}
EPSG_WGS84 = 4326

_TOOLS_ROOT = Path(__file__).resolve().parents[1] / "tools"


def _load_qa_contract():
    """Load the shared QA contract from a checkout or the installed wheel."""
    candidate = _TOOLS_ROOT / "deliverable_qa.py"
    if candidate.is_file():
        spec = importlib.util.spec_from_file_location("deliverable_qa", candidate)
        if spec is not None and spec.loader is not None:
            module = importlib.util.module_from_spec(spec)
            sys.modules.setdefault("deliverable_qa", module)
            spec.loader.exec_module(module)
            return module
    import deliverable_qa

    return deliverable_qa


_QA = _load_qa_contract()
REQUIRED_LAYERS = _QA.REQUIRED_LAYER_NAMES


class FootprintProvider(str, Enum):
    """Building-footprint source. Microsoft remains the backward-compatible default."""

    MICROSOFT = "microsoft"
    GOOGLE_OPEN_BUILDINGS = "google-open-buildings"


class ContainmentPolicyChoice(str, Enum):
    """What to do when one footprint encloses another."""

    NESTED = "nested"
    ABSORB = "absorb"
    ABSORB_WHEN_COINCIDENT = "absorb_when_coincident"


class TargetPrecision(str, Enum):
    """Google Open Buildings published confidence tier (an *estimate*, not a guarantee)."""

    P80 = "80"
    P85 = "85"
    P90 = "90"

    @property
    def tier(self) -> int:
        """Numeric precision tier the builder expects on the command line."""
        return int(self.value)


class CadGenerationResult(BaseModel):
    """Stable structured result returned by ``generate_cad_plan``."""

    ok: bool
    output_dir: str
    report_path: str
    dxf_path: str
    latitude: float
    longitude: float
    scale: str
    sheet_size: str
    study_radius_m: float
    footprint_provider: str
    confidence_policy: str
    target_precision: int | None = None
    applied_confidence_thresholds: list[float] = Field(default_factory=list)
    resolved_s2_cell_ids: list[str] = Field(default_factory=list)
    qa_contract_version: str
    containment_policy: str = "nested"
    nested_pairs_reported: int = 0
    warning: str = "Input imagery/data are not a cadastral survey; inspect provenance and QA report."


class TopologyReport(BaseModel):
    """Read-only structural checks for an R2018 metric DXF."""

    path: str
    valid: bool
    dxf_version: str
    epsg: int | None
    metric_units: bool
    missing_layers: list[str]
    paper_layouts: list[str]
    closed_building_rings: int
    invalid_building_rings: int
    road_centerline_entities: int
    qa_contract_version: str
    diagnostics: list[str]


class SupportedFormats(BaseModel):
    """CAD and print outputs that the engine can produce."""

    formats: list[str]
    sheets: list[str]
    scales: list[str]
    footprint_providers: list[str]
    target_precisions: list[int]
    source_capabilities: dict[str, dict[str, object]] = Field(default_factory=dict)


def _builder_path() -> Path:
    """Resolve the builder both from a checkout and from the installed wheel."""
    checkout = _TOOLS_ROOT / "cad-fond-de-plan-build.py"
    if checkout.is_file():
        return checkout
    packaged = Path(str(files("tools").joinpath("cad-fond-de-plan-build.py")))
    if packaged.is_file():
        return packaged
    raise FileNotFoundError("Packaged tools/cad-fond-de-plan-build.py is missing")


def _study_radius(sheet: str, denominator: int) -> float:
    """Choose an 80%-viewport circle so coordinate-only MCP requests fit at scale."""
    paper_w, paper_h = SHEETS[sheet]
    ground_w = (paper_w - 20.0) * denominator / 1000.0
    ground_h = (paper_h - 20.0 - 50.0) * denominator / 1000.0
    return round(min(ground_w, ground_h) * 0.40, 3)


def _validate_coordinate(lat: float, lon: float) -> None:
    if not (-90.0 <= lat <= 90.0):
        raise ValueError("lat must be in [-90, 90]")
    if not (-180.0 <= lon <= 180.0):
        raise ValueError("lon must be in [-180, 180]")


def _require_file(label: str, raw: str | None) -> Path | None:
    if raw is None:
        return None
    candidate = Path(raw).expanduser().resolve()
    if not candidate.exists():
        raise FileNotFoundError(f"{label} does not exist: {candidate}")
    return candidate


def _build_command(
    *,
    lat: float,
    lon: float,
    radius: float,
    output_dir: Path,
    provider: FootprintProvider,
    sheet_size: str,
    denominator: int,
    google_min_confidence: float,
    target_precision: TargetPrecision | None,
    footprint_file: Path | None,
    osm_file: Path | None,
    thresholds_file: Path | None,
) -> list[str]:
    command = [
        sys.executable,
        str(_builder_path()),
        "--lat",
        str(lat),
        "--lon",
        str(lon),
        "--radius",
        str(radius),
        "--output",
        str(output_dir),
        "--layout-sheet",
        sheet_size,
        "--layout-scale",
        str(denominator),
        "--footprint-provider",
        provider.value,
        "--report-detail",
        "summary",
        "--json",
    ]
    if provider is FootprintProvider.GOOGLE_OPEN_BUILDINGS:
        command += ["--google-min-confidence", str(google_min_confidence)]
        if target_precision is not None:
            command += ["--google-target-precision", str(target_precision.tier)]
        if thresholds_file is not None:
            command += ["--google-thresholds-path", str(thresholds_file)]
        if footprint_file is not None:
            command += ["--google-open-buildings-file", str(footprint_file)]
    elif footprint_file is not None:
        command += ["--microsoft-file", str(footprint_file)]
    if osm_file is not None:
        command += ["--osm-file", str(osm_file)]
    return command


@mcp.tool()
def generate_cad_plan(
    lat: Annotated[float, Field(ge=-90, le=90, description="Latitude in WGS84 degrees")],
    lon: Annotated[float, Field(ge=-180, le=180, description="Longitude in WGS84 degrees")],
    scale: Annotated[str, Field(pattern=r"^1:(250|500|1000)$", description="Printed map scale")],
    sheet_size: Annotated[str, Field(pattern=r"^(A3|A1|A0)$", description="Landscape ISO sheet size")],
    study_radius_m: Annotated[
        float | None, Field(gt=0, le=5000, description="Study radius in metres; omit for the sheet-fitted default")
    ] = None,
    footprint_provider: Annotated[
        FootprintProvider, Field(description="Footprint source; microsoft is the backward-compatible default")
    ] = FootprintProvider.MICROSOFT,
    google_min_confidence: Annotated[
        float, Field(ge=0.0, le=1.0, description="Baseline/fallback Google score cutoff")
    ] = 0.65,
    google_target_precision: Annotated[
        TargetPrecision | None,
        Field(description="Google S2-cell precision tier (estimated); omit to keep the baseline cutoff"),
    ] = None,
    footprint_file: Annotated[
        str | None,
        Field(description="Cached source: Microsoft .csv.gz tile, or Google .csv/.csv.gz tile or directory"),
    ] = None,
    osm_file: Annotated[str | None, Field(description="Cached OSM XML or GeoPackage")] = None,
    google_thresholds_file: Annotated[
        str | None, Field(description="Cached Google score-thresholds CSV; useful for offline runs")
    ] = None,
    containment_policy: Annotated[
        ContainmentPolicyChoice,
        Field(
            description=(
                "Behaviour when one footprint encloses another: nested (default, retain both and "
                "report), absorb, or absorb_when_coincident"
            )
        ),
    ] = ContainmentPolicyChoice.NESTED,
) -> CadGenerationResult:
    """Generate a metric CAD plan around a coordinate at a requested sheet scale.

    This tool invokes the existing builder only when explicitly called. It selects a
    conservative circular study radius that fits the requested paper viewport and
    writes each run to a new, non-overwriting directory under CAD_FOND_DE_PLAN_OUTPUT_ROOT.
    """
    _validate_coordinate(lat, lon)
    if scale not in SCALES:
        raise ValueError(f"Unsupported scale {scale!r}; choose {', '.join(SCALES)}")
    if sheet_size not in SHEETS:
        raise ValueError(f"Unsupported sheet {sheet_size!r}; choose A3, A1, or A0")
    if google_target_precision is not None and footprint_provider is not FootprintProvider.GOOGLE_OPEN_BUILDINGS:
        raise ValueError("google_target_precision requires footprint_provider='google-open-buildings'")
    if google_thresholds_file is not None and footprint_provider is not FootprintProvider.GOOGLE_OPEN_BUILDINGS:
        raise ValueError("google_thresholds_file requires footprint_provider='google-open-buildings'")
    if google_target_precision is not None and google_thresholds_file is None:
        # A tier without a cached table would trigger a network fetch mid-call.
        raise ValueError(
            "google_target_precision requires google_thresholds_file so region resolution stays offline; "
            "omit both to use the baseline cutoff"
        )

    resolved_footprint_file = _require_file("footprint_file", footprint_file)
    resolved_osm_file = _require_file("osm_file", osm_file)
    resolved_thresholds = _require_file("google_thresholds_file", google_thresholds_file)

    denominator = SCALES[scale]
    radius = float(study_radius_m) if study_radius_m is not None else _study_radius(sheet_size, denominator)
    output_root = Path(os.environ.get("CAD_FOND_DE_PLAN_OUTPUT_ROOT", "~/site-plan/mcp-runs")).expanduser().resolve()
    output_dir = output_root / f"run-{uuid.uuid4().hex}"
    output_dir.mkdir(parents=True, exist_ok=False)
    command = _build_command(
        lat=lat,
        lon=lon,
        radius=radius,
        output_dir=output_dir,
        provider=footprint_provider,
        sheet_size=sheet_size,
        denominator=denominator,
        google_min_confidence=google_min_confidence,
        target_precision=google_target_precision,
        footprint_file=resolved_footprint_file,
        osm_file=resolved_osm_file,
        thresholds_file=resolved_thresholds,
    )
    command += ["--containment-policy", containment_policy.value]
    try:
        completed = subprocess.run(
            command, cwd=_builder_path().parents[1], capture_output=True, text=True, timeout=900, check=False
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("CAD generation exceeded the 900-second limit") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "builder failed").strip()[-2000:]
        raise RuntimeError(f"CAD builder exited {completed.returncode}: {detail}")
    try:
        report = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Builder completed but returned invalid report JSON") from exc
    if not report.get("ok"):
        raise RuntimeError(f"CAD QA failed: {report.get('findings', [])}")
    footprints = report.get("footprints") or {}
    return CadGenerationResult(
        ok=True,
        output_dir=str(output_dir),
        report_path=str(output_dir / "fond-de-plan-report.json"),
        dxf_path=str(output_dir / "fond-de-plan-cad.dxf"),
        latitude=lat,
        longitude=lon,
        scale=scale,
        sheet_size=sheet_size,
        study_radius_m=radius,
        footprint_provider=footprints.get("provider", footprint_provider.value),
        confidence_policy=footprints.get("threshold_resolution_summary") or "single global minimum-confidence cutoff",
        target_precision=footprints.get("target_precision"),
        applied_confidence_thresholds=footprints.get("applied_confidence_thresholds") or [],
        resolved_s2_cell_ids=footprints.get("resolved_s2_cell_ids") or [],
        qa_contract_version=_QA.QA_CONTRACT_VERSION,
        containment_policy=str(containment_policy.value),
        nested_pairs_reported=int(((footprints.get("reconciliation") or {}).get("nested_retained")) or 0),
    )


@mcp.tool()
def verify_topology(
    dxf_path: Annotated[
        str, Field(min_length=5, description="Path to an R2018 DXF under CAD_FOND_DE_PLAN_ALLOWED_ROOT")
    ],
) -> TopologyReport:
    """Read a DXF and verify units, required layers, layouts, and building-ring validity.

    Uses the same QA contract as ``tools/check.py``, so both report identical
    findings for the same file.
    """
    allowed_root = Path(os.environ.get("CAD_FOND_DE_PLAN_ALLOWED_ROOT", "~/site-plan")).expanduser().resolve()
    candidate = Path(dxf_path).expanduser().resolve()
    if not candidate.is_relative_to(allowed_root):
        raise ValueError(f"DXF must be inside the configured allowed root: {allowed_root}")
    if not candidate.is_file() or candidate.suffix.lower() != ".dxf":
        raise ValueError("dxf_path must name an existing .dxf file")

    report_path = candidate.with_name("fond-de-plan-report.json")
    sibling_report = _QA.load_report(report_path) if report_path.is_file() else None
    expected_layouts = _QA.ExpectedLayoutResolver.from_report(sibling_report) if sibling_report else None
    result = _QA.qa_dxf_file(candidate, expected_layouts)
    layer_names = set()
    try:
        import ezdxf

        doc = ezdxf.readfile(candidate)
        layer_names = {layer.dxf.name for layer in doc.layers}
    except Exception:  # noqa: BLE001 - qa_dxf_file already reported the failure
        layer_names = set()
    return TopologyReport(
        path=str(candidate),
        valid=result["ok"],
        dxf_version=result["dxf_version"],
        epsg=result["epsg"],
        metric_units=result["metric_units"],
        missing_layers=sorted(_QA.REQUIRED_LAYER_NAMES - layer_names),
        paper_layouts=result["paper_layouts"],
        closed_building_rings=result["closed_building_rings"],
        invalid_building_rings=result["invalid_building_rings"],
        road_centerline_entities=result["road_centerline_entities"],
        qa_contract_version=_QA.QA_CONTRACT_VERSION,
        diagnostics=result["findings"],
    )


@mcp.tool()
def list_supported_formats() -> SupportedFormats:
    """List drawing, print, sheet, scale, and provider options with declared capabilities."""
    capabilities: dict[str, dict[str, object]] = {}
    stub = _TOOLS_ROOT / "footprint_providers.py"
    if stub.is_file():
        spec = importlib.util.spec_from_file_location("mcp_footprint_providers", stub)
        if spec is not None and spec.loader is not None:
            module = importlib.util.module_from_spec(spec)
            try:
                spec.loader.exec_module(module)
                capabilities = module.provider_capabilities()
            except Exception:  # noqa: BLE001 - capabilities are advisory metadata
                capabilities = {}
    return SupportedFormats(
        formats=["DXF R2018", "SVG", "PDF", "PNG (300 dpi)"],
        sheets=["A3 landscape", "A1 landscape", "A0 landscape"],
        scales=["1:250", "1:500", "1:1000"],
        footprint_providers=[member.value for member in FootprintProvider],
        target_precisions=[80, 85, 90],
        source_capabilities=capabilities,
    )


def main() -> None:
    """Run the official MCP stdio transport."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
