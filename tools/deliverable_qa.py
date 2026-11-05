#!/usr/bin/env python3
"""Single QA contract for Fond de Plan deliverables.

Every consumer — the builder's in-process QA, ``tools/check.py``, and the MCP
``verify_topology`` tool — calls this module instead of re-implementing checks.
That guarantees one verdict per artifact set.

The module is self-contained on purpose: it imports only the standard library at
module scope and defers ``ezdxf``/``Pillow`` to the functions that need them, so
it can be loaded by path from a checkout, by package import from an installed
wheel, or via ``importlib`` from ``server/mcp_server.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

QA_CONTRACT_VERSION = "1.0"

HOLLOW_SVG = "fond-de-plan-hollow.svg"
HATCHED_SVG = "fond-de-plan-hatched.svg"
DXF = "fond-de-plan-cad.dxf"
PNG = "fond-de-plan-sheet-300dpi.png"
PDF = "fond-de-plan-sheet-300dpi.pdf"
REPORT = "fond-de-plan-report.json"

DRAWING_DELIVERABLES = (HOLLOW_SVG, HATCHED_SVG, DXF, PNG, PDF)
ALL_DELIVERABLES = (*DRAWING_DELIVERABLES, REPORT)

# Architectural layer schema: name -> required DXF attributes.
REQUIRED_LAYERS: dict[str, dict[str, Any]] = {
    "BUILDINGS": {"lineweight": 50, "color": 7},
    "HATCH": {"lineweight": 13, "color": 7},
    "ROADS": {"lineweight": 35, "color": 7},
    "PARCELS": {"lineweight": 18, "color": 7},
    "SITE_BOUNDARY": {"lineweight": 60, "color": 7, "linetype": "SITE_DASH"},
    "G-BLDG-WALL": {"lineweight": 50, "color": 7},
    "G-BLDG-SHARED": {"lineweight": 50, "color": 7},
    "G-ROAD-CURB": {"lineweight": 35, "color": 7},
    "G-ROAD-CL": {"lineweight": 18, "color": 7, "linetype": "CENTER"},
    "G-PARCEL": {"lineweight": 18, "color": 7},
    "G-HATCH": {"lineweight": 13, "color": 7},
    "G-SITE-BOUNDARY": {"lineweight": 60, "color": 7, "linetype": "SITE_DASH"},
    "G-ANNO-GRID": {"lineweight": 13, "color": 7},
    "G-ANNO-NORTH": {"lineweight": 25, "color": 7},
    "G-ANNO-TTLB": {"lineweight": 35, "color": 7},
}

REQUIRED_LAYER_NAMES = frozenset(REQUIRED_LAYERS)
ANNOTATION_BLOCKS = ("GRID_CROSS", "NORTH_ARROW")
ALL_LAYOUTS = frozenset({"A3-1_250", "A1-1_500", "A0-1_1000"})
DEFAULT_PNG_PX = 4961  # 420 mm square at 300 dpi
MIN_DELIVERABLE_BYTES = 100


class ExpectedLayoutResolver:
    """Resolve which paper layouts an artifact set must contain.

    ``report`` declarations win, so a single-layout MCP run is judged against
    what it said it produced rather than against the full A3/A1/A0 set. When no
    declaration exists, the conservative legacy expectation applies.
    """

    @staticmethod
    def sheet_label_to_layout(label: str) -> str:
        """Map a human sheet label (``"A3 1:250"``) to a layout name (``"A3-1_250"``)."""
        sheet, _, scale = label.strip().partition(" ")
        if not sheet or not scale:
            return label.strip().replace(" ", "-").replace(":", "_")
        return f"{sheet}-{scale.replace(':', '_')}"

    @classmethod
    def from_report(cls, report: dict[str, Any] | None) -> frozenset[str]:
        if not report:
            return ALL_LAYOUTS
        declared = (report.get("paper_layouts") or {}).get("sheets") or []
        if not declared:
            return ALL_LAYOUTS
        return frozenset(cls.sheet_label_to_layout(str(label)) for label in declared)

    @classmethod
    def expected_png_px(cls, report: dict[str, Any] | None) -> int:
        if not report:
            return DEFAULT_PNG_PX
        sheet_mm = (report.get("outputs") or {}).get("sheet_mm")
        try:
            return round(float(sheet_mm) / 25.4 * 300)
        except (TypeError, ValueError):
            return DEFAULT_PNG_PX


def _svg_findings(path: Path, sheet_mm: float | None) -> list[str]:
    findings: list[str] = []
    expected = {f"{sheet_mm:g}mm", f"{sheet_mm:.1f}mm", f"{float(sheet_mm):g}mm"} if sheet_mm else {"420mm", "420.0mm"}
    try:
        root = ET.parse(path).getroot()
    except Exception as exc:  # noqa: BLE001 - report any parse failure as a finding
        return [f"{path.name}: SVG parse failed: {type(exc).__name__}: {exc}"]
    width, height = root.attrib.get("width"), root.attrib.get("height")
    if width not in expected or height not in expected:
        findings.append(f"{path.name}: page must be {sheet_mm or 420:g}mm square, found {width}x{height}")
    for element in root.iter():
        tag = element.tag.rsplit("}", 1)[-1]
        if tag in {"text", "image", "use"}:
            findings.append(f"{path.name}: prohibited {tag} element")
        for key in ("fill", "stroke"):
            value = element.attrib.get(key)
            if value and value not in {"#FFFFFF", "#000000", "none", "currentColor"}:
                findings.append(f"{path.name}: non-monochrome {key}={value}")
    return findings


def _layer_findings(doc, layer_names: set[str]) -> list[str]:
    findings: list[str] = []
    for name, required in REQUIRED_LAYERS.items():
        if name not in layer_names:
            findings.append(f"DXF missing required layer {name}")
            continue
        layer = doc.layers.get(name)
        lineweight = required.get("lineweight")
        if lineweight is not None and layer.dxf.lineweight != lineweight:
            findings.append(f"DXF {name} lineweight is {layer.dxf.lineweight}, expected {lineweight}/100 mm")
        color = required.get("color")
        if color is not None and layer.dxf.color != color:
            findings.append(f"DXF {name} color is {layer.dxf.color}, expected ACI {color} (monochrome)")
        linetype = required.get("linetype")
        if linetype is not None and layer.dxf.linetype != linetype:
            findings.append(f"DXF {name} linetype is {layer.dxf.linetype}, expected {linetype}")
    return findings


def _modelspace_findings(doc) -> tuple[list[str], bool]:
    """Return model-space findings and whether road centerlines are present."""
    findings: list[str] = []
    has_centerlines = False
    for entity in doc.modelspace():
        kind = entity.dxftype()
        if kind in {"TEXT", "MTEXT"}:
            findings.append(f"DXF includes model-space {kind}; annotations belong in paper space")
            break
        if kind == "INSERT" and entity.dxf.name != "GRID_CROSS":
            findings.append(f"DXF includes unapproved model-space block {entity.dxf.name}")
            break
    for entity in doc.modelspace():
        if entity.dxftype() in {"LINE", "LWPOLYLINE", "POLYLINE"} and entity.dxf.layer == "G-ROAD-CL":
            has_centerlines = True
            break
    return findings, has_centerlines


def qa_dxf_file(path: Path, expected_layouts: frozenset[str] | None = None) -> dict[str, Any]:
    """Validate one R2018 DXF against the shared contract."""
    findings: list[str] = []
    layouts: list[str] = []
    closed_rings = invalid_rings = centerlines = 0
    epsg = None
    dxf_version = ""
    metric_units = False
    try:
        import ezdxf
        from shapely.geometry import Polygon

        doc = ezdxf.readfile(path)
        dxf_version = doc.dxfversion
        metric_units = doc.header.get("$INSUNITS") == 6
        layer_names = {layer.dxf.name for layer in doc.layers}
        findings.extend(_layer_findings(doc, layer_names))
        modelspace_issues, has_centerlines = _modelspace_findings(doc)
        findings.extend(modelspace_issues)
        if has_centerlines:
            missing_blocks = [name for name in ANNOTATION_BLOCKS if name not in doc.blocks]
            if missing_blocks:
                findings.append(
                    f"DXF with road centerlines is missing annotation block(s): {', '.join(missing_blocks)}"
                )
        layouts = sorted(layout.name for layout in doc.layouts if layout.name != "Model")
        expected = ALL_LAYOUTS if expected_layouts is None else expected_layouts
        if not layouts:
            findings.append("No paper-space layout tabs found")
        elif not expected.issubset(set(layouts)):
            findings.append(f"DXF paper layouts incomplete: found {sorted(layouts)}, expected {sorted(expected)}")
        if doc.dxfversion != "AC1032":
            findings.append(f"DXF is not R2018/AC1032 ({doc.dxfversion})")
        if not metric_units:
            findings.append("DXF modelspace units are not metres ($INSUNITS)")
        epsg_value = doc.header.get("$USERI1")
        epsg = int(epsg_value) if epsg_value is not None else None
        if epsg is None:
            findings.append("DXF user header lacks an EPSG code")
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
        if invalid_rings:
            findings.append(f"{invalid_rings} closed building rings are invalid")
        if closed_rings == 0:
            findings.append("No closed building rings found on G-BLDG-WALL")
    except Exception as exc:  # noqa: BLE001 - surface any reader failure as a finding
        findings.append(f"DXF parse failed: {type(exc).__name__}: {exc}")
    return {
        "ok": not findings,
        "findings": findings,
        "dxf_version": dxf_version,
        "epsg": epsg,
        "metric_units": metric_units,
        "paper_layouts": layouts,
        "closed_building_rings": closed_rings,
        "invalid_building_rings": invalid_rings,
        "road_centerline_entities": centerlines,
    }


def _png_findings(path: Path, expected_px: int) -> list[str]:
    findings: list[str] = []
    try:
        from PIL import Image

        with Image.open(path) as image:
            if image.size != (expected_px, expected_px):
                findings.append(f"{path.name}: dimensions {image.size}, expected {expected_px}x{expected_px}")
            if round(image.info.get("dpi", (0, 0))[0]) != 300:
                findings.append(f"{path.name}: DPI {image.info.get('dpi')}, expected 300")
            if image.mode not in {"RGB", "RGBA"}:
                findings.append(f"{path.name}: unexpected color mode {image.mode}")
    except Exception as exc:  # noqa: BLE001
        findings.append(f"{path.name}: PNG parse failed: {type(exc).__name__}: {exc}")
    return findings


def _pdf_findings(path: Path) -> list[str]:
    try:
        if not path.read_bytes().startswith(b"%PDF-"):
            return [f"{path.name}: PDF signature invalid"]
    except Exception as exc:  # noqa: BLE001
        return [f"{path.name}: PDF read failed: {type(exc).__name__}: {exc}"]
    return []


def _report_findings(report: dict[str, Any], dxf_epsg: int | None) -> list[str]:
    findings: list[str] = []
    expected_epsg = (report.get("center") or {}).get("epsg")
    if not expected_epsg:
        findings.append("report missing projected EPSG")
    elif dxf_epsg is not None and dxf_epsg != expected_epsg:
        findings.append(f"DXF EPSG metadata {dxf_epsg} does not match report {expected_epsg}")
    if not (report.get("qa") or {}).get("ok"):
        findings.append("build report does not show a passing QA verdict")
    if not (report.get("building_regularization") or {}).get("output_buildings"):
        findings.append("report contains no regularized buildings")
    if not report.get("attribution"):
        findings.append("report missing source attribution")
    return findings


def load_report(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - absent/corrupt reports are handled by callers
        return None


def qa_output_directory(
    path: Path,
    *,
    include_report: bool = True,
    expected_layouts: frozenset[str] | list[str] | None = None,
    expected_png_px: int | None = None,
) -> dict[str, Any]:
    """Validate a deliverable directory with one consistent verdict.

    ``include_report=False`` supports the builder, which runs QA before writing
    the report and therefore passes ``expected_layouts`` and ``expected_png_px``
    explicitly. When a report is present and no override is given, the report's
    own declarations define the expectation.
    """
    path = Path(path).expanduser()
    if not path.exists():
        return {
            "ok": False,
            "path": str(path),
            "findings": [f"not found: {path}"],
            "qa_contract_version": QA_CONTRACT_VERSION,
        }
    if path.is_file():
        size = path.stat().st_size
        ok = size > MIN_DELIVERABLE_BYTES
        return {
            "ok": ok,
            "path": str(path),
            "bytes": size,
            "findings": [] if ok else ["file is empty or implausibly small"],
            "qa_contract_version": QA_CONTRACT_VERSION,
        }

    required = ALL_DELIVERABLES if include_report else DRAWING_DELIVERABLES
    findings = [f"missing deliverable: {name}" for name in required if not (path / name).is_file()]
    if findings:
        return {
            "ok": False,
            "path": str(path),
            "findings": findings,
            "qa_contract_version": QA_CONTRACT_VERSION,
        }

    report = load_report(path / REPORT) if include_report else None
    resolved_layouts = (
        frozenset(expected_layouts) if expected_layouts is not None else ExpectedLayoutResolver.from_report(report)
    )
    resolved_px = expected_png_px if expected_png_px is not None else ExpectedLayoutResolver.expected_png_px(report)
    sheet_mm = (report.get("outputs") or {}).get("sheet_mm") if report else None

    for name in (HOLLOW_SVG, HATCHED_SVG):
        findings.extend(_svg_findings(path / name, sheet_mm))
    dxf_result = qa_dxf_file(path / DXF, resolved_layouts)
    findings.extend(dxf_result["findings"])
    findings.extend(_png_findings(path / PNG, resolved_px))
    findings.extend(_pdf_findings(path / PDF))
    if include_report and report is not None:
        findings.extend(_report_findings(report, dxf_result.get("epsg")))

    return {
        "ok": not findings,
        "path": str(path),
        "findings": findings,
        "qa_contract_version": QA_CONTRACT_VERSION,
        "expected_layouts": sorted(resolved_layouts),
        "expected_png_px": resolved_px,
        "dxf": {
            "version": dxf_result["dxf_version"],
            "epsg": dxf_result["epsg"],
            "metric_units": dxf_result["metric_units"],
            "paper_layouts": dxf_result["paper_layouts"],
            "closed_building_rings": dxf_result["closed_building_rings"],
            "road_centerline_entities": dxf_result["road_centerline_entities"],
        },
        "deliverables": {name: (path / name).stat().st_size for name in required},
    }
