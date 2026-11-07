"""Paper-space annotation composited into the rendered SVG/PDF/PNG sheet.

The DXF has always carried a cartouche, a north arrow and a UTM grid in its
paperspace layout, but the raster and PDF renders showed the drawing alone: the
title-block band the page reserves simply came back blank. Anyone plotting the PDF
therefore got an untitled sheet and had to open AutoCAD to recover the annotation.

This module draws the same annotation into the SVG sheet so that all four
artifacts describe one page.

Two things had to be got right, and both are easy to get silently wrong:

**Which edge the cartouche sits on.** ``SheetPlacement.usable_rect_mm`` measures y
downward from the page top (it is consumed by an SVG ``viewBox``), while the DXF
``SheetLayout`` measures y upward from the page bottom. Copying ``cartouche_rect_mm``
straight into the SVG would put the title block at the top of the sheet while the
DXF put it at the bottom — the same page drawn two different ways. The cartouche is
therefore anchored to the page's true bottom edge, in SVG coordinates.

**Which UTM values the grid labels.** The DXF labels the viewport it was given. The
raster page may cover less ground than the DXF viewport (a small study radius on a
large sheet is bounded by the radius, not the paper), so the grid here is derived
from the ground the page actually covers. Labelling the paper extent instead would
print coordinates that do not match the drawing beneath them.
"""

from __future__ import annotations

import math
from typing import Any

#: The SVG QA contract forbids ``<text>``, ``<image>`` and ``<use>`` elements, and that
#: rule is right: a sheet handed to a plotter must not depend on which fonts the RIP
#: happens to have. Every string on this sheet is therefore converted to glyph outlines
#: at build time, so the PDF carries the letters as vectors and plots identically
#: everywhere. DejaVu Sans is bundled with matplotlib and metrically stable.
_FONT_FAMILY = "DejaVu Sans"


def text_to_path(text: str, x_mm: float, y_mm_baseline: float, height_mm: float, scale_x: float = 1.0) -> str:
    """Convert ``text`` to an SVG path ``d`` string of filled glyph outlines.

    ``height_mm`` is the cap height the caller wants on paper; matplotlib sizes fonts by
    em square, so the value is normalised against the glyph bounds to hit that target
    rather than silently printing at an em height nobody asked for.
    """
    from matplotlib.font_manager import FontProperties
    from matplotlib.textpath import TextPath

    if not text or not text.strip():
        return ""
    props = FontProperties(family=_FONT_FAMILY)
    # Unit-em path, then rescaled below.
    path = TextPath((0.0, 0.0), text, size=1.0, prop=props)
    extents = path.get_extents()
    if extents.height <= 0:
        return ""
    # Normalise so the glyph box is exactly height_mm tall, then flip y (SVG is y-down).
    k = height_mm / extents.height

    def pt(vertex) -> str:
        vx = vertex[0] * k * scale_x
        vy = -(vertex[1] - extents.y0) * k
        return f"{x_mm + vx:.4f},{y_mm_baseline + vy:.4f}"

    commands: list[str] = []
    # Quadratic (CURVE3) segments arrive as a control point followed by an endpoint, so
    # a pair of CURVE3 vertices becomes one SVG "Q". Copying the raw codes across
    # verbatim would emit two separate Q commands and distort the glyph.
    pending_control: tuple | None = None
    for vertex, code in zip(path.vertices, path.codes, strict=True):
        if code == 3:  # CURVE3
            if pending_control is None:
                pending_control = vertex
            else:
                commands.append(f"Q {pt(pending_control)} {pt(vertex)}")
                pending_control = None
        else:
            pending_control = None
            if code == 1:  # MOVETO
                commands.append(f"M {pt(vertex)}")
            elif code == 2:  # LINETO
                commands.append(f"L {pt(vertex)}")
            elif code == 4:  # CURVE4: control, control, endpoint
                # Handled by the caller collecting three vertices; not emitted by
                # DejaVu Sans, but a raised error beats a silently wrong outline.
                raise ValueError(f"cubic glyph segments are unsupported for {text!r}")
            elif code == 79:  # CLOSEPOLY
                commands.append("Z")
    return " ".join(commands)


def text_width_mm(text: str, height_mm: float) -> float:
    """Advance width of ``text`` at ``height_mm``, for aligning/limiting labels."""
    from matplotlib.font_manager import FontProperties
    from matplotlib.textpath import TextPath

    if not text or not text.strip():
        return 0.0
    path = TextPath((0.0, 0.0), text, size=1.0, prop=FontProperties(family=_FONT_FAMILY))
    extents = path.get_extents()
    if extents.height <= 0:
        return 0.0
    return extents.width * (height_mm / extents.height)


def add_vector_text(dwg, text: str, x_mm: float, baseline_mm: float, height_mm: float, group_id: str | None = None):
    """Add ``text`` to the drawing as one filled path of glyph outlines."""
    d = text_to_path(text, x_mm, baseline_mm, height_mm)
    if not d:
        return None
    attrs = {"fill": "#000000", "stroke": "none"}
    if group_id:
        attrs["id"] = group_id
    return dwg.add(dwg.path(d=d, **attrs))


#: Grid spacing in metres, keyed by plot denominator. Matches the DXF paperspace
#: rule (50 m up to 1:500, 100 m beyond it) so both artifacts annotate alike.
def grid_spacing_m(scale: int) -> float:
    return 50.0 if scale <= 500 else 100.0


def covered_ground_m(placement, pad_m: float = 0.0) -> tuple[float, float, float, float]:
    """Ground rectangle the page actually draws, in model metres (minx, miny, maxx, maxy).

    The page covers ``ground_extent_m``, but a run is bounded by its study radius, so
    the drawing stops at whichever is smaller. That smaller rectangle is what the grid
    must annotate.
    """
    ground_w, ground_h = placement.ground_extent_m
    half_w = min(ground_w, 2 * pad_m) / 2.0 if pad_m else ground_w / 2.0
    half_h = min(ground_h, 2 * pad_m) / 2.0 if pad_m else ground_h / 2.0
    cx, cy = placement.content_center_m
    return (cx - half_w, cy - half_h, cx + half_w, cy + half_h)


def cartouche_rect_svg(placement) -> tuple[float, float, float, float]:
    """Cartouche rectangle as ``(x, y, w, h)`` in SVG coordinates (y down from top).

    Anchored to the page's bottom edge, exactly as ``SheetLayout.cartouche_rect_mm``
    anchors it at y=``margin_mm`` measured up from the bottom. The conversion between
    the two conventions is ``y_svg = page_h - y_layout - h``.

    This is deliberately *not* ``page_h - margin - h``: the map band in the rendered
    page does not end 10 mm above the paper edge, so anchoring to the paper margin
    would push the title block up over the drawing.
    """
    width, height = placement.titleblock_mm
    margin = placement.margin_mm
    return (
        placement.page_w_mm - margin - width,
        placement.page_h_mm - margin - height,
        width,
        height,
    )


def map_band_svg(placement) -> tuple[float, float, float, float]:
    """Map band actually drawn by the geometry, as ``(x, y, w, h)`` in SVG millimetres.

    The renderer maps model y to SVG y with ``oy - y*mm_per_m`` where ``oy`` was
    computed for a y-up page, so the geometry occupies exactly
    ``y = margin .. page_h - margin - titleblock_h`` measured from the SVG origin.
    ``SheetPlacement.usable_rect_mm`` reports the same band in the mirrored y-up
    convention, which is why it cannot be used directly as an SVG rectangle.
    """
    margin = placement.margin_mm
    _, _, w, h = placement.usable_rect_mm
    return (margin, margin, w, h)


def scale_bar_rect_svg(placement) -> tuple[float, float, float, float]:
    """True 50 m scale bar, placed in the free strip below the map band.

    The strip is ``titleblock_h`` tall between the map band and the paper edge, so the
    bar is centred in it. Putting it inside the cartouche itself would collide with the
    title text at 1:1000, where a true 50 m bar is 50 mm long.
    """
    bar_len_mm = 50_000.0 / placement.scale
    margin = placement.margin_mm
    strip_top = placement.page_h_mm - margin - placement.titleblock_mm[1]
    height = 6.0
    return (
        margin + 5.0,
        strip_top + (placement.titleblock_mm[1] - height) / 2.0,
        bar_len_mm,
        height,
    )


def _model_to_svg(x_m: float, y_m: float, placement, ground) -> tuple[float, float]:
    """Model metres to SVG millimetres using the same mapping the geometry uses.

    ``placement.origin_mm`` plus ``mm_point`` is the single source of truth for where
    model space lands on paper; reusing it (rather than deriving a fresh affine map)
    is what keeps the grid on top of the drawing it annotates.
    """
    ox, oy = placement.origin_mm
    mm_per_m = placement.mm_per_m
    return ox + x_m * mm_per_m, oy - y_m * mm_per_m


def add_sheet_annotations(
    dwg,
    placement,
    metadata: dict[str, Any],
    *,
    pad_m: float = 0.0,
    with_grid: bool = True,
    with_north: bool = True,
    with_cartouche: bool = True,
):
    """Composite cartouche, scale bar, UTM grid and north arrow into an SVG sheet.

    Returns the list of group ids written, so callers and tests can assert the sheet
    actually received them rather than trusting a boolean.
    """
    written: list[str] = []
    ground = covered_ground_m(placement, pad_m)
    minx, miny, maxx, maxy = ground

    if with_grid:
        spacing = grid_spacing_m(placement.scale)
        first_e = math.ceil(minx / spacing) * spacing
        first_n = math.ceil(miny / spacing) * spacing
        e_values = [first_e + i * spacing for i in range(int((maxx - first_e) // spacing) + 1)]
        n_values = [first_n + i * spacing for i in range(int((maxy - first_n) // spacing) + 1)]

        grid = dwg.g(id="G-ANNO-GRID", fill="none", stroke="#000000", stroke_width="0.13")
        # Tick marks along the map's bottom edge, and their easting labels beneath it.
        for easting in e_values:
            gx, gy_bottom = _model_to_svg(easting, miny, placement, ground)
            grid.add(dwg.line(start=(gx, gy_bottom), end=(gx, gy_bottom - 2.0)))
        dwg.add(grid)
        written.append("G-ANNO-GRID")

        for easting in e_values:
            gx, gy_bottom = _model_to_svg(easting, miny, placement, ground)
            add_vector_text(dwg, f"E {easting:.0f}", gx + 0.8, gy_bottom + 1.6, 1.5)
        for northing in n_values:
            gx_right, gy = _model_to_svg(maxx, northing, placement, ground)
            # Northing labels read bottom-to-top along the right edge, matching the
            # 90-degree rotation the DXF paperspace uses for the same labels.
            add_vector_text(dwg, f"N {northing:.0f}", gx_right + 1.2, gy + text_width_mm(f"N {northing:.0f}", 1.5), 1.5)
        written.append("G-ANNO-GRID-LABELS")

    if with_north:
        # Same fixed paper size as the DXF NORTH_ARROW block: 17 mm tall, 6 mm wide.
        # Anchored to the map band's top-right corner rather than to the last drawn
        # vertex: at 1:1000 the drawn extent runs to the band edge, so pinning it to the
        # geometry pushed the arrowhead off the paper.
        # The arrow is 17 mm tall and its "N" sits 1.2 mm above that, so the base needs
        # 20.2 mm of clearance below the band's top edge or the arrowhead prints off the
        # paper. (Measured: a base 6 mm down put the tip at y = -1.0 mm.)
        bx, by, bw, _ = placement.map_band_mm
        nx = bx + bw - 12.0
        ny = by + 22.0
        north = dwg.g(id="G-ANNO-NORTH", fill="none", stroke="#000000", stroke_width="0.25")
        north.add(
            dwg.polygon(
                points=[
                    (nx, ny - 17.0),
                    (nx - 3.0, ny - 5.0),
                    (nx, ny),
                    (nx + 3.0, ny - 5.0),
                ]
            )
        )
        north.add(dwg.line(start=(nx, ny - 15.0), end=(nx, ny)))
        dwg.add(north)
        written.append("G-ANNO-NORTH")

        add_vector_text(dwg, "N", nx - 0.8, ny - 18.2, 2.2, group_id="G-ANNO-NORTH-LABEL")
        written.append("G-ANNO-NORTH-LABEL")

    if with_cartouche:
        from layout_engine import cartouche_text

        x, y, w, h = cartouche_rect_svg(placement)
        # Group stroke width is in mm because the viewBox is a millimetre rectangle.
        g = dwg.g(id="G-ANNO-TTLB", fill="none", stroke="#000000", stroke_width="0.35")
        g.add(dwg.rect(insert=(x, y), size=(w, h)))
        for offset in (10.0, 21.0, 32.0, 41.0):
            g.add(dwg.line(start=(x, y + offset), end=(x + w, y + offset), stroke_width="0.18"))
        g.add(dwg.line(start=(x + 90.0, y + 10.0), end=(x + 90.0, y + h), stroke_width="0.18"))
        lines = cartouche_text(metadata, placement.scale)
        # Baseline offsets measured down from the cartouche top, with a small descent
        # allowance so text sits inside its cell rather than on the rule below it.
        dwg.add(g)

        # Text goes on its own layer above the rules so glyph fills never sit under a
        # stroked cell line; both are pure black, but stacking keeps the outlines crisp.
        # Each line is fitted to its cell rather than allowed to run past the rule.
        # At 2.4 mm the UTM line is 91.7 mm wide in an 87 mm cell, so a fixed type size
        # prints coordinates straight through the cartouche border.
        def fitted(text: str, cell_w: float, nominal_mm: float = 2.4) -> float:
            width = text_width_mm(text, nominal_mm)
            if width <= cell_w:
                return nominal_mm
            return nominal_mm * (cell_w / width)

        left_cell_w = 87.0  # x+3 .. x+90
        right_cell_w = 84.0  # x+93 .. x+177
        baselines = [y + 7.2, y + 18.2, y + 29.2, y + 39.2]
        for text, baseline in zip(lines[:4], baselines, strict=True):
            add_vector_text(dwg, text, x + 3.0, baseline, fitted(text, left_cell_w))
        add_vector_text(dwg, lines[4], x + 93.0, y + 18.2, fitted(lines[4], right_cell_w))
        written.append("G-ANNO-TTLB")

        bx, by, bw, bh = scale_bar_rect_svg(placement)
        bar = dwg.g(id="G-ANNO-SCALE-BAR", stroke="#000000", stroke_width="0.18")
        segments = 10
        segment_w = bw / segments
        for i in range(segments):
            bar.add(
                dwg.rect(
                    insert=(bx + i * segment_w, by),
                    size=(segment_w, bh),
                    fill="#000000" if i % 2 == 0 else "#FFFFFF",
                )
            )
        dwg.add(bar)
        written.append("G-ANNO-SCALE-BAR")

        add_vector_text(
            dwg,
            "0     5     10    15    20    25    30    35    40    45    50 m",
            bx,
            by + bh + 2.6,
            2.0,
            group_id="G-ANNO-SCALE-BAR-LABEL",
        )
        written.append("G-ANNO-SCALE-BAR-LABEL")

    return written
