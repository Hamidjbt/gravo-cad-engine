"""Typed MCP façade for generation and read-only DXF topology inspection."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from importlib.resources import files
from pathlib import Path
from typing import Annotated

import ezdxf
from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field
from shapely.geometry import Polygon

mcp = FastMCP("cad-fond-de-plan")
SCALES = {"1:250": 250, "1:500": 500, "1:1000": 1000}
SHEETS = {"A3": (420.0, 297.0), "A1": (841.0, 594.0), "A0": (1189.0, 841.0)}
REQUIRED_LAYERS = {
    "G-BLDG-WALL",
    "G-BLDG-SHARED",
    "G-ROAD-CURB",
    "G-ROAD-CL",
    "G-PARCEL",
    "G-HATCH",
    "G-SITE-BOUNDARY",
    "G-ANNO-GRID",
    "G-ANNO-NORTH",
    "G-ANNO-TTLB",
}


class CadGenerationResult(BaseModel):
    """Stable structured result returned by `generate_cad_plan`."""

    ok: bool
    output_dir: str
    report_path: str
    dxf_path: str
    latitude: float
    longitude: float
    scale: str
    sheet_size: str
    study_radius_m: float
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
    diagnostics: list[str]


class SupportedFormats(BaseModel):
    """CAD and print outputs that the engine can produce."""

    formats: list[str]
    sheets: list[str]
    scales: list[str]


def _builder_path() -> Path:
    """Resolve the builder both from a checkout and from the installed wheel."""
    checkout = Path(__file__).resolve().parents[1] / "tools" / "cad-fond-de-plan-build.py"
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


@mcp.tool()
def generate_cad_plan(
    lat: Annotated[float, Field(ge=-90, le=90, description="Latitude in WGS84 degrees")],
    lon: Annotated[float, Field(ge=-180, le=180, description="Longitude in WGS84 degrees")],
    scale: Annotated[str, Field(pattern=r"^1:(250|500|1000)$", description="Printed map scale")],
    sheet_size: Annotated[str, Field(pattern=r"^(A3|A1|A0)$", description="Landscape ISO sheet size")],
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

    denominator = SCALES[scale]
    radius = _study_radius(sheet_size, denominator)
    output_root = Path(os.environ.get("CAD_FOND_DE_PLAN_OUTPUT_ROOT", "~/site-plan/mcp-runs")).expanduser().resolve()
    output_dir = output_root / f"run-{uuid.uuid4().hex}"
    output_dir.mkdir(parents=True, exist_ok=False)
    builder = _builder_path()
    command = [
        sys.executable,
        str(builder),
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
        "--json",
    ]
    try:
        completed = subprocess.run(
            command, cwd=builder.parents[1], capture_output=True, text=True, timeout=900, check=False
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
    )


@mcp.tool()
def verify_topology(
    dxf_path: Annotated[
        str, Field(min_length=5, description="Path to an R2018 DXF under CAD_FOND_DE_PLAN_ALLOWED_ROOT")
    ],
) -> TopologyReport:
    """Read a DXF and verify units, required layers, layouts, and building-ring validity."""
    allowed_root = Path(os.environ.get("CAD_FOND_DE_PLAN_ALLOWED_ROOT", "~/site-plan")).expanduser().resolve()
    candidate = Path(dxf_path).expanduser().resolve()
    if not candidate.is_relative_to(allowed_root):
        raise ValueError(f"DXF must be inside the configured allowed root: {allowed_root}")
    if not candidate.is_file() or candidate.suffix.lower() != ".dxf":
        raise ValueError("dxf_path must name an existing .dxf file")

    doc = ezdxf.readfile(candidate)
    layer_names = {layer.dxf.name for layer in doc.layers}
    missing_layers = sorted(REQUIRED_LAYERS - layer_names)
    layouts = sorted(layout.name for layout in doc.layouts if layout.name != "Model")
    closed_rings = invalid_rings = centerlines = 0
    diagnostics: list[str] = []
    for entity in doc.modelspace():
        if entity.dxftype() == "LWPOLYLINE" and entity.dxf.layer == "G-BLDG-WALL" and entity.closed:
            closed_rings += 1
            coords = [(point[0], point[1]) for point in entity.get_points("xy")]
            if len(coords) < 4 or not Polygon(coords).is_valid:
                invalid_rings += 1
        elif entity.dxftype() == "POLYLINE" and entity.dxf.layer == "G-BLDG-WALL" and entity.is_closed:
            closed_rings += 1
            coords = [(vertex.dxf.location.x, vertex.dxf.location.y) for vertex in entity.vertices]
            if len(coords) < 4 or not Polygon(coords).is_valid:
                invalid_rings += 1
        if entity.dxftype() in {"LINE", "LWPOLYLINE", "POLYLINE"} and entity.dxf.layer == "G-ROAD-CL":
            centerlines += 1

    metric_units = doc.header.get("$INSUNITS") == 6
    if doc.dxfversion != "AC1032":
        diagnostics.append(f"Expected R2018/AC1032, found {doc.dxfversion}")
    if not metric_units:
        diagnostics.append("$INSUNITS is not metres")
    if missing_layers:
        diagnostics.append(f"Missing required layers: {', '.join(missing_layers)}")
    if not layouts:
        diagnostics.append("No paper-space layout tabs found")
    if invalid_rings:
        diagnostics.append(f"{invalid_rings} closed building rings are invalid")
    if closed_rings == 0:
        diagnostics.append("No closed building rings found on G-BLDG-WALL")
    valid = not diagnostics
    epsg_value = doc.header.get("$USERI1")
    return TopologyReport(
        path=str(candidate),
        valid=valid,
        dxf_version=doc.dxfversion,
        epsg=int(epsg_value) if epsg_value is not None else None,
        metric_units=metric_units,
        missing_layers=missing_layers,
        paper_layouts=layouts,
        closed_building_rings=closed_rings,
        invalid_building_rings=invalid_rings,
        road_centerline_entities=centerlines,
        diagnostics=diagnostics,
    )


@mcp.tool()
def list_supported_formats() -> SupportedFormats:
    """List available drawing, print, sheet, and scale output options."""
    return SupportedFormats(
        formats=["DXF R2018", "SVG", "PDF", "PNG (300 dpi)"],
        sheets=["A3 landscape", "A1 landscape", "A0 landscape"],
        scales=["1:250", "1:500", "1:1000"],
    )


def main() -> None:
    """Run the official MCP stdio transport."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
