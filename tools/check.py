#!/usr/bin/env python3
"""Deterministic package QA for a Fond de Plan export. JSON stdout; 0 pass, 1 fail."""
import argparse
import json
from pathlib import Path
import sys
from xml.etree import ElementTree as ET


def run(path: Path) -> dict:
    if not path.exists():
        return {"ok": False, "path": str(path), "findings": [f"not found: {path}"]}
    if path.is_file():
        return {"ok": path.stat().st_size > 100, "path": str(path), "bytes": path.stat().st_size,
                "findings": [] if path.stat().st_size > 100 else ["file is empty or implausibly small"]}
    required = ["fond-de-plan-hollow.svg", "fond-de-plan-hatched.svg", "fond-de-plan-cad.dxf",
                "fond-de-plan-sheet-300dpi.png", "fond-de-plan-sheet-300dpi.pdf", "fond-de-plan-report.json"]
    findings = [f"missing deliverable: {n}" for n in required if not (path/n).is_file()]
    dxf_epsg = None
    if findings:
        return {"ok": False, "path": str(path), "findings": findings}
    try:
        for name in required[:2]:
            root = ET.parse(path/name).getroot()
            if root.attrib.get("width") not in ("420mm", "420.0mm") or root.attrib.get("height") not in ("420mm", "420.0mm"):
                findings.append(f"{name}: page must be 420mm square")
            for el in root.iter():
                tag = el.tag.rsplit("}", 1)[-1]
                if tag in ("text", "image", "use"):
                    findings.append(f"{name}: prohibited {tag} element")
                for key in ("fill", "stroke"):
                    value = el.attrib.get(key)
                    if value and value not in ("#FFFFFF", "#000000", "none", "currentColor"):
                        findings.append(f"{name}: non-monochrome {key}={value}")
    except Exception as exc:
        findings.append(f"SVG parse: {type(exc).__name__}: {exc}")
    try:
        import ezdxf
        doc = ezdxf.readfile(path/"fond-de-plan-cad.dxf")
        layers = {layer.dxf.name for layer in doc.layers}
        expected_lineweights = {"BUILDINGS": 50, "HATCH": 13, "ROADS": 35, "PARCELS": 18, "SITE_BOUNDARY": 60}
        for name, lineweight in expected_lineweights.items():
            if name not in layers:
                findings.append(f"DXF missing {name} layer")
            elif doc.layers.get(name).dxf.lineweight != lineweight:
                findings.append(f"DXF {name} lineweight is not {lineweight}/100 mm")
        if doc.header.get("$INSUNITS") != 6:
            findings.append("DXF modelspace units are not metres")
        dxf_epsg = doc.header.get("$USERI1")
        if dxf_epsg is None:
            findings.append("DXF user header lacks an EPSG code")
        architectural_layers = {
            "G-BLDG-WALL": 50, "G-BLDG-SHARED": 50, "G-ROAD-CURB": 35,
            "G-ROAD-CL": 18, "G-PARCEL": 18, "G-HATCH": 13,
            "G-SITE-BOUNDARY": 60, "G-ANNO-GRID": 13, "G-ANNO-NORTH": 25,
            "G-ANNO-TTLB": 35,
        }
        for name, lineweight in architectural_layers.items():
            if name not in layers:
                findings.append(f"DXF missing architectural layer {name}")
            elif doc.layers.get(name).dxf.lineweight != lineweight:
                findings.append(f"DXF {name} lineweight is not {lineweight}/100 mm")
            elif doc.layers.get(name).dxf.color != 7:
                findings.append(f"DXF {name} is not monochrome ACI 7")
        if "G-ROAD-CL" in layers and doc.layers.get("G-ROAD-CL").dxf.linetype != "CENTER":
            findings.append("DXF G-ROAD-CL must use CENTER linetype")
        if "G-SITE-BOUNDARY" in layers and doc.layers.get("G-SITE-BOUNDARY").dxf.linetype != "SITE_DASH":
            findings.append("DXF G-SITE-BOUNDARY must use SITE_DASH linetype")
        if doc.dxfversion != "AC1032":
            findings.append(f"DXF is not R2018/AC1032 ({doc.dxfversion})")
        if "GRID_CROSS" not in doc.blocks or "NORTH_ARROW" not in doc.blocks:
            findings.append("DXF missing reusable GRID_CROSS or NORTH_ARROW block")
        for ent in doc.modelspace():
            if ent.dxftype() in ("TEXT", "MTEXT"):
                findings.append(f"DXF includes model-space {ent.dxftype()}; annotations belong in paper space")
                break
            if ent.dxftype() == "INSERT" and ent.dxf.name != "GRID_CROSS":
                findings.append(f"DXF includes unapproved model-space block {ent.dxf.name}")
                break
        paper_layouts = {layout.name for layout in doc.layouts if layout.name != "Model"}
        required_layouts = {"A3-1_250", "A1-1_500", "A0-1_1000"}
        if paper_layouts and not required_layouts.issubset(paper_layouts):
            findings.append(f"DXF paper layouts incomplete: {sorted(paper_layouts)}")
    except Exception as exc:
        findings.append(f"DXF parse: {type(exc).__name__}: {exc}")
    try:
        from PIL import Image
        with Image.open(path/"fond-de-plan-sheet-300dpi.png") as im:
            if im.size != (4961, 4961):
                findings.append(f"PNG dimensions {im.size}, expected 4961x4961")
            if round(im.info.get("dpi", (0, 0))[0]) != 300:
                findings.append(f"PNG DPI {im.info.get('dpi')}, expected 300")
    except Exception as exc:
        findings.append(f"PNG parse: {type(exc).__name__}: {exc}")
    try:
        if not (path/"fond-de-plan-sheet-300dpi.pdf").read_bytes().startswith(b"%PDF-"):
            findings.append("PDF signature invalid")
    except Exception as exc:
        findings.append(f"PDF read: {type(exc).__name__}: {exc}")
    try:
        report = json.loads((path/"fond-de-plan-report.json").read_text())
        expected_epsg = report.get("center", {}).get("epsg")
        if not expected_epsg:
            findings.append("report missing projected EPSG")
        elif dxf_epsg != expected_epsg:
            findings.append(f"DXF EPSG metadata {dxf_epsg} does not match report {expected_epsg}")
        if not report.get("qa", {}).get("ok"):
            findings.append("build report does not show a passing QA verdict")
        if not report.get("building_regularization", {}).get("output_buildings"):
            findings.append("report contains no regularized buildings")
        if not report.get("attribution"):
            findings.append("report missing source attribution")
    except Exception as exc:
        findings.append(f"report parse: {type(exc).__name__}: {exc}")
    return {"ok": not findings, "path": str(path), "findings": findings,
            "deliverables": {n: (path/n).stat().st_size for n in required}}


def main():
    parser = argparse.ArgumentParser(description="Validate Fond de Plan deliverables")
    parser.add_argument("path", help="output directory or file")
    parser.add_argument("--json", action="store_true", help="emit JSON (default also JSON)")
    args = parser.parse_args()
    result = run(Path(args.path).expanduser())
    print(json.dumps(result, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
