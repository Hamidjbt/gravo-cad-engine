#!/usr/bin/env python3
"""Crop the existing Fond de Plan SVG to a centered A3 landscape 1:250 sheet.

The source map is read-only. Building/road/parcel/site path coordinates are copied as-is;
only the SVG viewBox/layout is changed. Hatch strokes are re-generated from the unchanged
building polygons at the original paper-space pitch so the 0.13 mm pen and 3.5 px/96dpi
spacing remain correct after zooming.
"""

from __future__ import annotations
import argparse
import copy
import hashlib
import json
import math
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from PIL import Image
import cairosvg
from shapely.geometry import GeometryCollection, LineString, Polygon, MultiLineString

SVG_NS = "http://www.w3.org/2000/svg"
ET.register_namespace("", SVG_NS)
A3_W_MM, A3_H_MM = 420.0, 297.0
SCALE_DENOMINATOR = 250.0
PAPER_HATCH_MM = 3.5 * 25.4 / 96.0
NUM_RE = r"[-+]?(?:\d*\.\d+|\d+\.?\d*)"
TOKEN_RE = re.compile(rf"([MLZ])|({NUM_RE})", re.I)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def crop_window(cx: float, cy: float) -> tuple[float, float, float, float, float]:
    # Base SVG has 0.7 page units per ground metre; a 1:250 print has 4mm/m.
    crop_w = A3_W_MM * 0.7 * (SCALE_DENOMINATOR / 1000.0)
    crop_h = A3_H_MM * 0.7 * (SCALE_DENOMINATOR / 1000.0)
    zoom = A3_W_MM / crop_w
    return cx - crop_w / 2.0, cy - crop_h / 2.0, crop_w, crop_h, zoom


def parse_path_rings(d: str) -> list[list[tuple[float, float]]]:
    tokens = [m.group(0) for m in TOKEN_RE.finditer(d)]
    rings: list[list[tuple[float, float]]] = []
    current: list[tuple[float, float]] = []
    i = 0
    command = None
    while i < len(tokens):
        t = tokens[i]
        if t.upper() in ("M", "L", "Z"):
            command = t.upper()
            i += 1
            if command == "Z":
                if current:
                    rings.append(current)
                    current = []
                command = None
                continue
            continue
        if command not in ("M", "L") or i + 1 >= len(tokens):
            raise ValueError(f"Unsupported SVG path command near {tokens[i : i + 4]}")
        x, y = float(tokens[i]), float(tokens[i + 1])
        current.append((x, y))
        i += 2
        if command == "M":
            command = "L"  # subsequent coordinate pairs are implicit lineto
    if current:
        rings.append(current)
    return rings


def polygon_from_path(d: str) -> Polygon | None:
    rings = parse_path_rings(d)
    if not rings or len(rings[0]) < 3:
        return None
    shell = rings[0]
    holes = [ring for ring in rings[1:] if len(ring) >= 3]
    poly = Polygon(shell, holes)
    if not poly.is_valid:
        poly = poly.buffer(0)
    return poly if isinstance(poly, Polygon) and not poly.is_empty else None


def path_d_for_lines(geom) -> str:
    pieces = []
    if isinstance(geom, LineString):
        lines = [geom]
    elif isinstance(geom, (MultiLineString, GeometryCollection)):
        lines = [g for g in geom.geoms if isinstance(g, LineString)]
    else:
        lines = []
    for line in lines:
        pts = list(line.coords)
        if len(pts) < 2:
            continue
        pieces.append(f"M {pts[0][0]:.5f},{pts[0][1]:.5f} " + " ".join(f"L {x:.5f},{y:.5f}" for x, y in pts[1:]))
    return " ".join(pieces)


def hatch_lines(poly: Polygon, spacing: float) -> list[LineString]:
    minx, miny, maxx, maxy = poly.bounds
    cs = [y - x for x, y in poly.exterior.coords]
    for ring in poly.interiors:
        cs.extend(y - x for x, y in ring.coords)
    step = spacing * math.sqrt(2.0)
    c = math.floor(min(cs) / step) * step
    end = max(cs)
    extent = math.hypot(maxx - minx, maxy - miny) + 10.0
    out = []
    while c <= end + step:
        probe = LineString([(minx - extent, minx - extent + c), (maxx + extent, maxx + extent + c)])
        inter = poly.intersection(probe)
        if isinstance(inter, LineString):
            if inter.length > 0.02:
                out.append(inter)
        elif isinstance(inter, (MultiLineString, GeometryCollection)):
            out.extend(g for g in inter.geoms if isinstance(g, LineString) and g.length > 0.02)
        c += step
    return out


def find_group(root: ET.Element, group_id: str) -> ET.Element:
    for el in root.iter(f"{{{SVG_NS}}}g"):
        if el.attrib.get("id") == group_id:
            return el
    raise ValueError(f"Source SVG is missing group {group_id}")


def scale_paper_strokes(el: ET.Element, zoom: float) -> None:
    for node in el.iter():
        sw = node.attrib.get("stroke-width")
        if sw is not None:
            node.set("stroke-width", f"{float(sw) / zoom:.8f}")
        dash = node.attrib.get("stroke-dasharray")
        if dash:
            vals = [float(v) for v in re.findall(NUM_RE, dash)]
            node.set("stroke-dasharray", " ".join(f"{v / zoom:.8f}" for v in vals))


def build(source: Path, output_dir: Path) -> dict:
    source = source.expanduser().resolve()
    output_dir = output_dir.expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    output_dir.mkdir(parents=True, exist_ok=True)
    before = sha256(source)
    source_root = ET.parse(source).getroot()
    original_view = [float(v) for v in source_root.attrib["viewBox"].split()]
    if len(original_view) != 4 or not math.isclose(original_view[2], 420.0, abs_tol=0.01):
        raise ValueError("Expected the unchanged 420 mm square base-map SVG")

    build_group = find_group(source_root, "BUILDINGS")
    building_paths = [el for el in build_group.findall(f"{{{SVG_NS}}}path") if el.attrib.get("d")]
    buildings = [p for el in building_paths if (p := polygon_from_path(el.attrib["d"])) is not None]
    if not buildings:
        raise ValueError("No existing building polygons found in source SVG")

    site_group = find_group(source_root, "SITE_BOUNDARY")
    site_paths = [el for el in site_group.findall(f"{{{SVG_NS}}}path") if el.attrib.get("d")]
    site_polys = [p for el in site_paths if (p := polygon_from_path(el.attrib["d"])) is not None]
    if not site_polys:
        raise ValueError("No existing site-boundary path found in source SVG")
    site = max(site_polys, key=lambda p: p.area)
    cx, cy = (site.bounds[0] + site.bounds[2]) / 2.0, (site.bounds[1] + site.bounds[3]) / 2.0

    # 1:250 model-to-paper scale: 1 map metre occupies 4 mm on paper.
    # The base SVG stores 0.7 viewBox units per map metre, so its crop is 73.5 x 51.975 units.
    x0, y0, crop_w, crop_h, zoom = crop_window(cx, cy)
    hatch_pitch_source = PAPER_HATCH_MM / zoom

    # Copy existing vector layers unchanged (their SVG stroke attributes are compensated
    # for the new viewBox so printed pen widths remain exactly 0.50/0.35/0.18/0.60 mm).
    layer_ids = ["ROADS", "PARCELS", "BUILDINGS", "BUILDING_WALLS_OVER_HATCH", "SITE_BOUNDARY"]
    copied = {gid: copy.deepcopy(find_group(source_root, gid)) for gid in layer_ids}
    for g in copied.values():
        scale_paper_strokes(g, zoom)

    # Regenerate only the hatch strokes from the unchanged base building polygons so the
    # hatch stays 3.5 px at 96 dpi after the 1:250 zoom. All lines are shapely-clipped.
    hatch_group = copy.deepcopy(find_group(source_root, "HATCH"))
    hatch_group.clear()
    hatch_group.attrib.update(
        {
            "id": "HATCH",
            "fill": "none",
            "stroke": "#000000",
            "stroke-linecap": "butt",
            "stroke-width": f"{0.13 / zoom:.8f}",
        }
    )
    hatch_count = 0
    for poly in buildings:
        for line in hatch_lines(poly, hatch_pitch_source):
            d = path_d_for_lines(line)
            if d:
                ET.SubElement(hatch_group, f"{{{SVG_NS}}}path", {"d": d})
                hatch_count += 1

    new_root = ET.Element(
        f"{{{SVG_NS}}}svg",
        {
            "width": "420mm",
            "height": "297mm",
            "viewBox": f"0 0 {crop_w:.6f} {crop_h:.6f}",
            "preserveAspectRatio": "xMidYMid meet",
            "version": "1.1",
            "baseProfile": "full",
        },
    )
    ET.SubElement(
        new_root,
        f"{{{SVG_NS}}}rect",
        {"x": "0", "y": "0", "width": f"{crop_w:.6f}", "height": f"{crop_h:.6f}", "fill": "#FFFFFF"},
    )
    # Normalize the SVG viewport to a zero origin; this avoids print-preview engines
    # treating the prior nonzero base-map coordinates as a tiny positioned drawing.
    content = ET.SubElement(
        new_root, f"{{{SVG_NS}}}g", {"id": "CROPPED_SITE_PLAN", "transform": f"translate({-x0:.6f} {-y0:.6f})"}
    )
    for gid in ["ROADS", "PARCELS", "BUILDINGS"]:
        content.append(copied[gid])
    content.append(hatch_group)
    content.append(copied["BUILDING_WALLS_OVER_HATCH"])
    content.append(copied["SITE_BOUNDARY"])

    svg_path = output_dir / "fond-de-plan-zoom-a3.svg"
    ET.ElementTree(new_root).write(svg_path, encoding="utf-8", xml_declaration=True)
    png_path = output_dir / "fond-de-plan-zoom-a3-300dpi.png"
    pdf_path = output_dir / "fond-de-plan-zoom-a3.pdf"
    png_w, png_h = round(A3_W_MM / 25.4 * 300), round(A3_H_MM / 25.4 * 300)
    cairosvg.svg2png(url=str(svg_path), write_to=str(png_path), output_width=png_w, output_height=png_h, dpi=300)
    with Image.open(png_path) as rendered:
        rgba = rendered.convert("RGBA")
        white = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        flattened = Image.alpha_composite(white, rgba).convert("RGB")
        flattened.save(png_path, dpi=(300, 300), optimize=True)
    cairosvg.svg2pdf(url=str(svg_path), write_to=str(pdf_path), dpi=96)

    after = sha256(source)
    if before != after:
        raise RuntimeError("Source base-map SVG changed during zoom layout; aborting")
    with Image.open(png_path) as im:
        if im.size != (png_w, png_h) or round(im.info.get("dpi", (0, 0))[0]) != 300 or im.mode != "RGB":
            raise RuntimeError(f"300-DPI opaque PNG verification failed: {im.size}, {im.info.get('dpi')}, {im.mode}")
    if not pdf_path.read_bytes().startswith(b"%PDF-"):
        raise RuntimeError("PDF signature check failed")

    return {
        "ok": True,
        "source_read_only": str(source),
        "source_sha256_unchanged": before == after,
        "source_geometry_preserved": True,
        "source_building_paths": len(building_paths),
        "buildings_used_for_hatch": len(buildings),
        "hatch_segments": hatch_count,
        "site_bbox_center_source_units": [cx, cy],
        "site_bbox_center_on_page_mm": [210.0, 148.5],
        "crop_viewbox_source_units": [x0, y0, crop_w, crop_h],
        "paper": {
            "size_mm": [420, 297],
            "orientation": "landscape",
            "scale": "1:250",
            "png_px": [png_w, png_h],
            "dpi": 300,
        },
        "pen_weights_mm": {"BUILDINGS": 0.50, "ROADS": 0.35, "PARCELS": 0.18, "HATCH": 0.13, "SITE_BOUNDARY": 0.60},
        "hatch_pitch_mm": PAPER_HATCH_MM,
        "outputs": [str(svg_path), str(pdf_path), str(png_path)],
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read-only crop of the existing Fond de Plan to centered A3 landscape at 1:250"
    )
    default_site_plan = Path.home() / "site-plan"
    parser.add_argument("--source", default=str(default_site_plan / "fond-de-plan-hatched.svg"))
    parser.add_argument("--output-dir", default=str(default_site_plan))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    try:
        result = build(Path(args.source), Path(args.output_dir))
        print(json.dumps(result, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
