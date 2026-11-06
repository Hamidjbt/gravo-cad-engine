#!/usr/bin/env python3
"""Parametric paper-space resolver and vector cartouche helpers.

Model-space geometry is expected in metres; paper-space primitives are in millimetres.
No file is written unless the caller explicitly invokes the CLI or a writer function.
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import asdict, dataclass
from typing import Any

# ISO 216 long/short edges in millimetres. Orientation is applied by page_size()
# rather than baked into the table, so a sheet name never implies an orientation.
ISO_216_MM = {"A3": (420.0, 297.0), "A1": (841.0, 594.0), "A0": (1189.0, 841.0)}
SHEETS = ISO_216_MM  # backwards-compatible alias: landscape edges (long, short)
ORIENTATIONS = ("landscape", "portrait")
STANDARD_SCALES = (250, 500, 1000)
TITLEBLOCK_MM = (180.0, 50.0)


def page_size(sheet: str, orientation: str = "landscape") -> tuple[float, float]:
    """Return the true ISO 216 page for ``sheet`` in ``orientation``.

    A3 landscape is 420 x 297 mm; the same sheet in portrait is 297 x 420 mm. The
    table stores long/short edges only, so "A3" can never silently mean "420 square".
    """
    key = sheet.upper()
    if key not in ISO_216_MM:
        raise ValueError(f"Unsupported sheet {sheet!r}; choose {', '.join(sorted(ISO_216_MM))}")
    if orientation not in ORIENTATIONS:
        raise ValueError(f"Orientation must be one of {ORIENTATIONS}; received {orientation!r}")
    long_edge, short_edge = ISO_216_MM[key]
    return (long_edge, short_edge) if orientation == "landscape" else (short_edge, long_edge)


@dataclass(frozen=True)
class SheetLayout:
    sheet: str
    width_mm: float
    height_mm: float
    scale: int
    margin_mm: float
    viewport_bounds_m: tuple[float, float, float, float]
    viewport_rect_mm: tuple[float, float, float, float]
    cartouche_rect_mm: tuple[float, float, float, float]
    scale_bar_rect_mm: tuple[float, float, float, float]
    geometry_center_m: tuple[float, float]
    ground_extent_m: tuple[float, float]
    orientation: str = "landscape"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def resolve_layout(
    bounds_m: tuple[float, float, float, float],
    sheet: str = "A3",
    scale: int = 250,
    margin_mm: float = 10.0,
    titleblock_mm: tuple[float, float] = TITLEBLOCK_MM,
    orientation: str = "landscape",
) -> SheetLayout:
    """Resolve a centered, exact-scale ISO 216 paper viewport.

    Raises rather than silently stretching geometry when a requested scale cannot contain
    the supplied model bounds. Cartouche is fixed 180x50mm; scale bar is a true 50m bar.
    ``orientation`` selects which ISO 216 edge is the page width; the cartouche and scale
    bar always sit on the bottom edge, so portrait pages are laid out, not rotated after
    the fact.
    """
    key = sheet.upper()
    if key not in ISO_216_MM:
        raise ValueError(f"Unsupported sheet {sheet!r}; choose A3, A1, or A0")
    if scale not in STANDARD_SCALES:
        raise ValueError(f"Scale must be one of {STANDARD_SCALES}")
    minx, miny, maxx, maxy = map(float, bounds_m)
    if not all(math.isfinite(v) for v in (minx, miny, maxx, maxy)) or maxx <= minx or maxy <= miny:
        raise ValueError("bounds_m must be a finite, non-empty (minx,miny,maxx,maxy) box")
    paper_w, paper_h = page_size(key, orientation)
    tb_w, tb_h = titleblock_mm
    usable_w = paper_w - 2 * margin_mm
    usable_h = paper_h - 2 * margin_mm - tb_h
    if min(usable_w, usable_h, tb_w, tb_h) <= 0 or tb_w > usable_w:
        raise ValueError("Margins/title block leave no usable map viewport")
    ground_w, ground_h = usable_w * scale / 1000.0, usable_h * scale / 1000.0
    geom_w, geom_h = maxx - minx, maxy - miny
    if geom_w > ground_w + 1e-8 or geom_h > ground_h + 1e-8:
        raise ValueError(
            f"Geometry {geom_w:.3f}x{geom_h:.3f}m does not fit {key} at 1:{scale}; choose a smaller denominator or larger sheet"
        )
    cx, cy = (minx + maxx) / 2.0, (miny + maxy) / 2.0
    view = (cx - ground_w / 2.0, cy - ground_h / 2.0, cx + ground_w / 2.0, cy + ground_h / 2.0)
    viewport = (margin_mm, margin_mm + tb_h, usable_w, usable_h)
    cartouche = (paper_w - margin_mm - tb_w, margin_mm, tb_w, tb_h)
    bar_len = 50_000.0 / scale
    if bar_len > usable_w - 10.0:
        raise ValueError("A true 50m scale bar does not fit the usable sheet width at this scale")
    scale_bar = (margin_mm + 5.0, margin_mm + (tb_h - 6.0) / 2.0, bar_len, 6.0)
    return SheetLayout(
        key,
        paper_w,
        paper_h,
        int(scale),
        float(margin_mm),
        view,
        viewport,
        cartouche,
        scale_bar,
        (cx, cy),
        (ground_w, ground_h),
        orientation,
    )


@dataclass(frozen=True)
class SheetPlacement:
    """Exact-scale mapping from model metres to millimetres on one printed page.

    A page at 1:N draws the ground at exactly ``1000/N`` millimetres per metre. Nothing
    here fits content to the paper: the scale is fixed by the page declaration, and a
    footprint either lands inside the usable area or is clipped. That distinction is the
    whole difference between a true-to-scale plot and a picture of one.

    ``custom`` exists for ``--sheet-mm`` runs that name no ISO sheet; it is catalogued as
    a custom square page rather than mislabelled "A3".
    """

    sheet: str
    orientation: str
    page_w_mm: float
    page_h_mm: float
    scale: int
    margin_mm: float
    origin_mm: tuple[float, float]
    content_m: tuple[float, float, float, float]
    titleblock_mm: tuple[float, float] = TITLEBLOCK_MM

    @property
    def mm_per_m(self) -> float:
        return 1000.0 / self.scale

    @property
    def usable_rect_mm(self) -> tuple[float, float, float, float]:
        """Usable map area, as (x, y, w, h) from the page's bottom-left.

        The cartouche band is reserved exactly as ``resolve_layout`` reserves it for the
        DXF paper space. Both artifacts describe one page, so both must report the same
        ground extent; a difference of 50 mm here is 50 m of ground at 1:1000.
        """
        tb_h = self.titleblock_mm[1]
        return (
            self.margin_mm,
            self.margin_mm + tb_h,
            self.page_w_mm - 2 * self.margin_mm,
            self.page_h_mm - 2 * self.margin_mm - tb_h,
        )

    @property
    def ground_extent_m(self) -> tuple[float, float]:
        """Ground covered by the usable area at this denominator."""
        _, _, w, h = self.usable_rect_mm
        return w * self.scale / 1000.0, h * self.scale / 1000.0

    @property
    def aspect_ratio(self) -> float:
        return self.page_w_mm / self.page_h_mm

    def to_dict(self) -> dict[str, Any]:
        return {
            "sheet": self.sheet,
            "orientation": self.orientation,
            "page_mm": {"width": self.page_w_mm, "height": self.page_h_mm},
            "aspect_ratio": round(self.aspect_ratio, 6),
            "scale": f"1:{self.scale}",
            "mm_per_m": round(self.mm_per_m, 6),
            "ground_extent_m": [round(v, 3) for v in self.ground_extent_m],
            "usable_rect_mm": [round(v, 3) for v in self.usable_rect_mm],
            "content_bounds_m": [round(v, 3) for v in self.content_m],
        }


def resolve_placement(
    content_bounds_m: tuple[float, float, float, float],
    *,
    sheet: str = "A3",
    scale: int = 500,
    orientation: str = "landscape",
    margin_mm: float = 10.0,
    page_mm_override: tuple[float, float] | None = None,
    titleblock_mm: tuple[float, float] = TITLEBLOCK_MM,
) -> SheetPlacement:
    """Place ``content_bounds_m`` on an ISO page at exactly 1:``scale``.

    The content is centred on the page. It is never rescaled to fit: callers that need a
    different fit must choose a different sheet or denominator.

    ``page_mm_override`` exists for ``--sheet-mm`` runs that name no ISO sheet. Such a
    page must be declared with ``sheet="custom"``; labelling a 420 mm square "A3" is the
    defect this release removes.
    """
    if page_mm_override is not None:
        if sheet.upper() != "CUSTOM":
            raise ValueError("page_mm_override requires sheet='custom'")
        page_w, page_h = (float(page_mm_override[0]), float(page_mm_override[1]))
    else:
        page_w, page_h = page_size(sheet, orientation)
    if scale <= 0:
        raise ValueError("Scale denominator must be positive")
    minx, miny, maxx, maxy = map(float, content_bounds_m)
    cx, cy = (minx + maxx) / 2.0, (miny + maxy) / 2.0
    mm_per_m = 1000.0 / scale
    tb_w, tb_h = titleblock_mm
    usable_w = page_w - 2 * margin_mm
    usable_h = page_h - 2 * margin_mm - tb_h
    if usable_w <= 0 or usable_h <= 0:
        raise ValueError("Margins/title block leave no usable map viewport")
    if tb_w > usable_w:
        raise ValueError("Title block is wider than the usable page width")
    return SheetPlacement(
        sheet=sheet.upper(),
        orientation=orientation,
        page_w_mm=page_w,
        page_h_mm=page_h,
        scale=int(scale),
        margin_mm=float(margin_mm),
        # Centre of the usable rect, expressed in the page's y-up convention: the map
        # occupies margin + titleblock .. page_h - margin.
        origin_mm=(
            margin_mm + usable_w / 2.0 - cx * mm_per_m,
            margin_mm + tb_h + usable_h / 2.0 + cy * mm_per_m,
        ),
        content_m=(minx, miny, maxx, maxy),
        titleblock_mm=(float(tb_w), float(tb_h)),
    )


def coverage_bounds(
    center_m: tuple[float, float], page: SheetPlacement, *, pad_m: float = 0.0
) -> tuple[float, float, float, float]:
    """Ground actually covered by ``page``, centred on ``center_m`` (a study-radius box).

    Returns the smaller of the page area and the requested pad, so a small radius on a
    large sheet is bounded by the radius, and a large radius on a small sheet is bounded
    by the paper.
    """
    ground_w, ground_h = page.ground_extent_m
    half_w = min(ground_w, 2 * pad_m) / 2.0 if pad_m else ground_w / 2.0
    half_h = min(ground_h, 2 * pad_m) / 2.0 if pad_m else ground_h / 2.0
    cx, cy = center_m
    return (cx - half_w, cy - half_h, cx + half_w, cy + half_h)


def resolve_sheet_set(
    bounds_m: tuple[float, float, float, float],
    scales: dict[str, int] | None = None,
    margin_mm: float = 10.0,
    orientation: str = "landscape",
) -> list[SheetLayout]:
    """Return explicitly-scaled A3/A1/A0 layouts; defaults use 1:250/1:500/1:1000."""
    scales = scales or {"A3": 250, "A1": 500, "A0": 1000}
    return [
        resolve_layout(bounds_m, sheet, scales[sheet], margin_mm, orientation=orientation)
        for sheet in ("A3", "A1", "A0")
    ]


def metadata_from_utm(
    easting_m: float,
    northing_m: float,
    epsg: int,
    ground_area_m2: float | None,
    title: str = "FOND DE PLAN — SITE DETAIL",
) -> dict[str, Any]:
    from pyproj import Transformer

    lon, lat = Transformer.from_crs(int(epsg), 4326, always_xy=True).transform(easting_m, northing_m)
    return {
        "title": title,
        "utm": {"epsg": f"EPSG:{int(epsg)}", "easting_m": float(easting_m), "northing_m": float(northing_m)},
        "wgs84": {"latitude": float(lat), "longitude": float(lon)},
        # D2: a run without --image has no site polygon, so there is no ground
        # area to state. cartouche_text already renders "NOT SPECIFIED" for None;
        # only this line used to crash the whole build with a bare TypeError.
        "ground_area_m2": float(ground_area_m2) if ground_area_m2 is not None else None,
    }


def cartouche_text(metadata: dict[str, Any], scale: int) -> list[str]:
    utm = metadata.get("utm", {})
    wgs = metadata.get("wgs84", {})
    return [
        str(metadata.get("title", "FOND DE PLAN — SITE DETAIL")),
        f"CENTER UTM {utm.get('epsg', 'EPSG:?')}: E {float(utm.get('easting_m', 0)):.3f}  N {float(utm.get('northing_m', 0)):.3f}",
        f"WGS84: {float(wgs.get('latitude', 0)):.6f}, {float(wgs.get('longitude', 0)):.6f}",
        (
            f"GROUND AREA: {float(metadata['ground_area_m2']):.1f} m2"
            if metadata.get("ground_area_m2") is not None
            else "GROUND AREA: NOT SPECIFIED"
        ),
        f"SCALE 1:{scale}  |  {metadata.get('date', '')}",
    ]


def add_cartouche_svg(dwg, layout: SheetLayout, metadata: dict[str, Any]):
    """Add DIN-style 180x50mm vector title block and true 0–50m bar to svgwrite.Drawing."""
    x, y, w, h = layout.cartouche_rect_mm
    g = dwg.g(id="G-ANNO-TTLB", fill="none", stroke="#000000", stroke_width="0.35")
    g.add(dwg.rect(insert=(x, y), size=(w, h)))
    for yy in (y + 10, y + 21, y + 32, y + 41):
        g.add(dwg.line(start=(x, yy), end=(x + w, yy), stroke_width="0.18"))
    g.add(dwg.line(start=(x + 90, y + 10), end=(x + 90, y + h), stroke_width="0.18"))
    lines = cartouche_text(metadata, layout.scale)
    positions = [(x + 3, y + 7), (x + 3, y + 18), (x + 3, y + 29), (x + 3, y + 39), (x + 93, y + 18)]
    for text, pos in zip(lines, positions, strict=True):
        g.add(dwg.text(text, insert=pos, fill="#000000", stroke="none", font_size="2.4mm", font_family="sans-serif"))
    bx, by, bw, bh = layout.scale_bar_rect_mm
    n = 10
    segment_w = bw / n
    for i in range(n):
        g.add(
            dwg.rect(
                insert=(bx + i * segment_w, by),
                size=(segment_w, bh),
                fill="#000000" if i % 2 == 0 else "#FFFFFF",
                stroke="#000000",
                stroke_width="0.18",
            )
        )
    g.add(
        dwg.text(
            "0     5     10    15    20    25    30    35    40    45    50 m",
            insert=(bx, by + bh + 3.0),
            fill="#000000",
            stroke="none",
            font_size="2.0mm",
        )
    )
    dwg.add(g)
    return g


def add_cartouche_dxf(layout, pspace, metadata: dict[str, Any]):
    """Add the same paper-space linework/text to an ezdxf Paperspace layout."""
    x, y, w, h = layout.cartouche_rect_mm
    layer = "G-ANNO-TTLB"
    pspace.add_lwpolyline(
        [(x, y), (x + w, y), (x + w, y + h), (x, y + h)],
        close=True,
        dxfattribs={"layer": layer, "const_width": 0.0, "lineweight": 35},
    )
    for yy in (y + 10, y + 21, y + 32, y + 41):
        pspace.add_line((x, yy), (x + w, yy), dxfattribs={"layer": layer, "lineweight": 18})
    pspace.add_line((x + 90, y + 10), (x + 90, y + h), dxfattribs={"layer": layer, "lineweight": 18})
    lines = cartouche_text(metadata, layout.scale)
    positions = [(x + 3, y + 5), (x + 3, y + 13), (x + 3, y + 24), (x + 3, y + 35), (x + 93, y + 13)]
    for text, pos in zip(lines, positions, strict=True):
        pspace.add_text(text, dxfattribs={"layer": layer, "height": 2.0, "insert": pos, "lineweight": 18})
    bx, by, bw, bh = layout.scale_bar_rect_mm
    for i in range(10):
        left = bx + i * bw / 10
        right = bx + (i + 1) * bw / 10
        if i % 2 == 0:
            pspace.add_solid(
                [(left, by), (right, by), (left, by + bh), (right, by + bh)], dxfattribs={"layer": layer, "color": 7}
            )
        pspace.add_lwpolyline(
            [(left, by), (right, by), (right, by + bh), (left, by + bh)],
            close=True,
            dxfattribs={"layer": layer, "lineweight": 18},
        )
    pspace.add_text(
        "0 5 10 15 20 25 30 35 40 45 50 m", dxfattribs={"layer": layer, "height": 1.6, "insert": (bx, by + bh + 2.0)}
    )


def ensure_annotation_blocks(doc) -> None:
    """Create reusable fixed-size paper-space UTM grid-cross and north-arrow blocks."""
    if "GRID_CROSS" not in doc.blocks:
        block = doc.blocks.new("GRID_CROSS", base_point=(0, 0))
        # Paper-space symbol: fixed 5 mm total arm span at every viewport scale.
        block.add_line((-2.5, 0), (2.5, 0), dxfattribs={"layer": "G-ANNO-GRID", "lineweight": 13})
        block.add_line((0, -2.5), (0, 2.5), dxfattribs={"layer": "G-ANNO-GRID", "lineweight": 13})
    if "NORTH_ARROW" not in doc.blocks:
        block = doc.blocks.new("NORTH_ARROW", base_point=(0, 0))
        block.add_lwpolyline(
            [(0, 0), (-3, 5), (0, 17), (3, 5)], close=True, dxfattribs={"layer": "G-ANNO-NORTH", "lineweight": 25}
        )
        block.add_line((0, 2), (0, 17), dxfattribs={"layer": "G-ANNO-NORTH", "lineweight": 25})
        block.add_text("N", dxfattribs={"layer": "G-ANNO-NORTH", "height": 2.2, "insert": (-1.3, 19)})


def add_grid_annotations(layout: SheetLayout, pspace, doc) -> None:
    """Annotate major UTM grid coordinates around the viewport in paper space."""
    minx, miny, maxx, maxy = layout.viewport_bounds_m
    vx, vy, vw, vh = layout.viewport_rect_mm
    spacing = 50.0 if layout.scale <= 500 else 100.0
    first_e = math.ceil(minx / spacing) * spacing
    first_n = math.ceil(miny / spacing) * spacing
    e_values = [first_e + i * spacing for i in range(int((maxx - first_e) // spacing) + 1)]
    n_values = [first_n + i * spacing for i in range(int((maxy - first_n) // spacing) + 1)]
    grid_layer = "G-ANNO-GRID"
    ensure_annotation_blocks(doc)
    for easting in e_values:
        paper_x = vx + (easting - minx) / (maxx - minx) * vw
        pspace.add_line((paper_x, vy), (paper_x, vy + 2.0), dxfattribs={"layer": grid_layer, "lineweight": 13})
        pspace.add_text(
            f"E {easting:.0f}",
            dxfattribs={"layer": grid_layer, "height": 1.5, "insert": (paper_x + 0.8, vy + vh + 1.2), "lineweight": 13},
        )
    for northing in n_values:
        paper_y = vy + (northing - miny) / (maxy - miny) * vh
        pspace.add_line(
            (vx + vw - 2.0, paper_y), (vx + vw, paper_y), dxfattribs={"layer": grid_layer, "lineweight": 13}
        )
        pspace.add_text(
            f"N {northing:.0f}",
            dxfattribs={
                "layer": grid_layer,
                "height": 1.5,
                "insert": (layout.width_mm - 2.0, paper_y + 0.8),
                "rotation": 90,
                "lineweight": 13,
            },
        )
    for easting in e_values:
        paper_x = vx + (easting - minx) / (maxx - minx) * vw
        for northing in n_values:
            paper_y = vy + (northing - miny) / (maxy - miny) * vh
            pspace.add_blockref("GRID_CROSS", (paper_x, paper_y), dxfattribs={"layer": grid_layer})
    pspace.add_blockref("NORTH_ARROW", (vx + vw - 12.0, vy + vh - 24.0), dxfattribs={"layer": "G-ANNO-NORTH"})


def add_paperspace_viewport(doc, layout: SheetLayout, metadata: dict[str, Any], layout_name: str | None = None):
    """Create an R2000+ paperspace tab, exact-scale viewport, and vector cartouche."""
    name = layout_name or f"{layout.sheet}-1_{layout.scale}"
    pspace = doc.layouts.new(name)
    pspace.page_setup(size=(layout.width_mm, layout.height_mm), margins=(0, 0, 0, 0), units="mm")
    vx, vy, vw, vh = layout.viewport_rect_mm
    pspace.add_viewport(
        center=(vx + vw / 2, vy + vh / 2),
        size=(vw, vh),
        view_center_point=layout.geometry_center_m,
        view_height=layout.ground_extent_m[1],
    )
    add_cartouche_dxf(layout, pspace, metadata)
    add_grid_annotations(layout, pspace, doc)
    return pspace


def main() -> int:
    p = argparse.ArgumentParser(description="Resolve standard A3/A1/A0 metric CAD sheet viewports")
    p.add_argument("--sheet", choices=tuple(SHEETS), default="A3")
    p.add_argument("--scale", type=int, choices=STANDARD_SCALES, default=250)
    p.add_argument("--bounds", nargs=4, type=float, required=True, metavar=("MINX", "MINY", "MAXX", "MAXY"))
    p.add_argument("--margin-mm", type=float, default=10.0)
    a = p.parse_args()
    print(json.dumps(resolve_layout(tuple(a.bounds), a.sheet, a.scale, a.margin_mm).to_dict(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
