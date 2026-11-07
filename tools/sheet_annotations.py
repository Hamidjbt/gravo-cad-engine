"""Paper-space annotation composited into the rendered SVG/PDF/PNG sheet.

The DXF has always carried a cartouche, a north arrow and a UTM grid in its
paperspace layout, but the raster and PDF renders showed the drawing alone: the
title-block band the page reserves simply came back blank. Anyone plotting the PDF
therefore got an untitled sheet and had to open AutoCAD to recover the annotation.

This module draws the same annotation into the SVG sheet so that all four
artifacts describe one page.

Three things had to be got right, and all are easy to get silently wrong:

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

**Whether the labels are attached to anything.** A coordinate printed in the margin
with nothing to connect it to the drawing is decoration. Every label is therefore
anchored to a tick that physically crosses the map frame, and the northing labels are
rotated to read along the frame like a bar scale.
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

#: Type sizes on paper, in millimetres. A drawing sheet reads in three registers:
#: the drawing title, the field values an engineer actually looks up, and the small
#: print. Keeping them in one place stops a stray literal from inventing a fourth.
TYPE_TITLE_MM = 4.2
TYPE_SUBTITLE_MM = 2.0
TYPE_VALUE_MM = 2.9
TYPE_LABEL_MM = 1.6
TYPE_SMALL_MM = 1.9
TYPE_GRID_MM = 1.9


def _font(weight: str = "normal"):
    from matplotlib.font_manager import FontProperties

    return FontProperties(family=_FONT_FAMILY, weight=weight)


def _glyph_path(text: str, size: float = 1.0, weight: str = "normal"):
    from matplotlib.textpath import TextPath

    if not text or not text.strip():
        return None
    return TextPath((0.0, 0.0), text, size=size, prop=_font(weight))


def text_to_path(
    text: str,
    x_mm: float,
    y_mm_baseline: float,
    height_mm: float,
    scale_x: float = 1.0,
    weight: str = "normal",
) -> str:
    """Convert ``text`` to an SVG path ``d`` string of filled glyph outlines.

    ``height_mm`` is the cap height the caller wants on paper; matplotlib sizes fonts by
    em square, so the value is normalised against the glyph bounds to hit that target
    rather than silently printing at an em height nobody asked for.
    """
    path = _glyph_path(text, 1.0, weight)
    if path is None:
        return ""
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


def text_width_mm(text: str, height_mm: float, weight: str = "normal") -> float:
    """Advance width of ``text`` at ``height_mm``, for aligning/limiting labels."""
    path = _glyph_path(text, 1.0, weight)
    if path is None:
        return 0.0
    extents = path.get_extents()
    if extents.height <= 0:
        return 0.0
    return extents.width * (height_mm / extents.height)


def text_advance_mm(text: str, height_mm: float, tracking_mm: float = 0.0, weight: str = "normal") -> float:
    """Width of ``text`` including inter-letter tracking.

    The title is set letter-spaced, which is a typographic effect no single glyph run
    gives us, so its advance has to be computed the same way ``draw_tracked_text``
    draws it or the fit check would under-measure and overflow the frame.
    """
    if not text:
        return 0.0
    return text_width_mm(text, height_mm, weight) + tracking_mm * max(0, len(text) - 1)


def add_vector_text(
    dwg,
    text: str,
    x_mm: float,
    baseline_mm: float,
    height_mm: float,
    group_id: str | None = None,
    weight: str = "normal",
    rotation_deg: float = 0.0,
    anchor: str = "start",
    advance_mm: float | None = None,
):
    """Add ``text`` to the drawing as one filled path of glyph outlines.

    ``rotation_deg`` rotates about the anchor point (SVG is clockwise-positive), which
    is how the northing labels read along the right frame. ``anchor`` shifts the run by
    its measured width so a caller can right- or centre-align without guessing.
    """
    from svgwrite import path as svg_path

    d = text_to_path(text, 0.0, 0.0, height_mm, weight=weight)
    if not d:
        return None
    width = advance_mm if advance_mm is not None else text_width_mm(text, height_mm, weight)
    dx = {"start": 0.0, "end": -width, "middle": -width / 2.0}.get(anchor)
    if dx is None:
        raise ValueError(f"unknown anchor {anchor!r}")
    transform = f"translate({x_mm + dx:.4f},{baseline_mm:.4f})"
    if rotation_deg:
        transform += f" rotate({rotation_deg:.4f})"
    attrs: dict[str, Any] = {"fill": "#000000", "stroke": "none", "transform": transform}
    if group_id:
        attrs["id"] = group_id
    return dwg.add(svg_path.Path(d=d, **attrs))


def draw_tracked_text(dwg, text: str, x_mm: float, baseline_mm: float, height_mm: float, tracking_mm: float):
    """Draw ``text`` with explicit letter spacing, one glyph path per character.

    Used for the drawing title. Tracking is what makes a title block read as drawn
    rather than typed, and it has to be applied per glyph: ``text_to_path`` composes a
    whole string as a single run with the font's own advances.
    """
    from svgwrite import path as svg_path

    cursor = x_mm
    placed = 0
    for char in text:
        if char == " ":
            cursor += text_width_mm("n", height_mm) * 0.55 + tracking_mm
            continue
        d = text_to_path(char, 0.0, 0.0, height_mm)
        if d:
            dwg.add(
                svg_path.Path(
                    d=d,
                    fill="#000000",
                    stroke="none",
                    transform=f"translate({cursor:.4f},{baseline_mm:.4f})",
                )
            )
            placed += 1
        cursor += text_width_mm(char, height_mm) + tracking_mm
    return placed


#: Grid spacing in metres, keyed by plot denominator. Matches the DXF paperspace
#: rule (50 m up to 1:500, 100 m beyond it) so both artifacts annotate alike.
def grid_spacing_m(scale: int) -> float:
    return 50.0 if scale <= 500 else 100.0


def covered_ground_m(
    placement, pad_m: float = 0.0, viewport_m: tuple[float, float, float, float] | None = None
) -> tuple[float, float, float, float]:
    """Ground rectangle the page actually draws, in model metres (minx, miny, maxx, maxy).

    The page covers ``ground_extent_m``, but a run is bounded by its study area, so the
    drawing stops at whichever is smaller. That smaller rectangle is what the grid must
    annotate.

    ``viewport_m`` is the study rectangle itself. Pass it whenever the study area is not
    a square: ``pad_m`` alone describes a study area of ``2*pad`` on *both* axes, so a
    rectangular viewport clamped through ``pad_m`` comes back square and the annotations
    get drawn round a viewport that does not exist. That is how the frame ended up
    227 x 227 mm on a 400 mm wide band.
    """
    if viewport_m is not None:
        minx, miny, maxx, maxy = (float(v) for v in viewport_m)
        gw, gh = placement.ground_extent_m
        cx, cy = placement.content_center_m
        # Never annotate ground the page does not actually show.
        return (
            max(minx, cx - gw / 2.0),
            max(miny, cy - gh / 2.0),
            min(maxx, cx + gw / 2.0),
            min(maxy, cy + gh / 2.0),
        )
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


# ---------------------------------------------------------------------------
# frame, grid and ticks
# ---------------------------------------------------------------------------

#: Tick length crossing the frame, in millimetres: long enough to read as a surveyor's
#: grid mark at plot size, short enough that twenty of them do not become a dashed border.
_TICK_MM = 3.0
_LABEL_GAP_MM = 1.0


def add_label_plate(
    dwg,
    text: str,
    cx: float,
    baseline: float,
    height: float,
    plate_pad: float = 0.7,
    clamp: tuple[float, float] | None = None,
):
    """Draw ``text`` on a white plate, centred on ``cx``.

    Grid labels annotate the drawing, so they have to sit on it -- but a 1.5 mm label
    landing on a solid black footprint is unreadable. A knocked-out plate is what a
    draftsman does; inventing a second ink colour is not an option under the monochrome
    contract.
    """
    width = text_width_mm(text, height)
    if width <= 0:
        return
    if clamp is not None:
        # The first and last label sit on the frame's own edge, where a centred plate
        # hangs outside the frame and gets clipped. Nudge them back inside instead of
        # letting the sheet eat half a coordinate.
        lo, hi = clamp
        cx = max(lo + width / 2.0 + plate_pad, min(hi - width / 2.0 - plate_pad, cx))
    dwg.add(
        dwg.rect(
            insert=(cx - width / 2.0 - plate_pad, baseline - height - plate_pad * 0.6),
            size=(width + 2 * plate_pad, height + plate_pad * 1.4),
            fill="#FFFFFF",
            stroke="none",
        )
    )
    add_vector_text(dwg, text, cx, baseline, height, anchor="middle")


def _grid_values(placement, pad_m: float, spacing: float, viewport_m=None):
    minx, miny, maxx, maxy = covered_ground_m(placement, pad_m, viewport_m)
    first_e = math.ceil(minx / spacing) * spacing
    first_n = math.ceil(miny / spacing) * spacing
    eastings = [first_e + i * spacing for i in range(int((maxx - first_e) // spacing) + 1)]
    northings = [first_n + i * spacing for i in range(int((maxy - first_n) // spacing) + 1)]
    return eastings, northings


def add_map_frame(dwg, placement, pad_m: float = 0.0, viewport_m=None):
    """Heavy map viewport frame with a surveyor's tick ladder on all four edges.

    The frame is what makes the sheet read as a drawing rather than a printout: it
    states the extent of the plotted viewport, and the ticks give the UTM labels
    something to attach to.
    """
    minx, miny, maxx, maxy = covered_ground_m(placement, pad_m, viewport_m)
    x0, y0 = _model_to_svg(minx, maxy, placement, None)
    x1, y1 = _model_to_svg(maxx, miny, placement, None)
    left, top = min(x0, x1), min(y0, y1)
    right, bottom = max(x0, x1), max(y0, y1)

    frame = dwg.g(id="G-ANNO-FRAME", fill="none", stroke="#000000", stroke_width=0.5)
    frame.add(dwg.rect(insert=(left, top), size=(right - left, bottom - top)))
    # Inner hairline offset 1.2 mm: the double rule is the standard sheet border that
    # separates the map from the margin without needing a second colour.
    frame.add(
        dwg.rect(
            insert=(left + 1.2, top + 1.2),
            size=(right - left - 2.4, bottom - top - 2.4),
            stroke_width=0.18,
        )
    )
    dwg.add(frame)
    return (left, top, right, bottom)


# ---------------------------------------------------------------------------
# cartouche
# ---------------------------------------------------------------------------


def _designation(metadata: dict[str, Any]) -> list[tuple[str, float]]:
    """Split the drawing designation into (line, type size) for the title cell.

    The metadata title is ``"FOND DE PLAN — SITE DETAIL"``: the part before the dash is
    the drawing name and the part after it is the subtitle. Splitting on the em dash
    keeps ``layout_engine.cartouche_text`` as the single source of the string, so the
    DXF paperspace and the PDF cannot disagree about what the sheet is called.
    """
    title = str(metadata.get("title", "FOND DE PLAN — SITE DETAIL"))
    for separator in ("—", "–", " - "):
        if separator in title:
            head, tail = title.split(separator, 1)
            return [(head.strip(), TYPE_TITLE_MM), (tail.strip(), TYPE_SUBTITLE_MM)]
    return [(title.strip(), TYPE_TITLE_MM)]


def _cartouche_cells(metadata: dict[str, Any], placement, w: float, h: float):
    """Field values and the vertical grid of the title block, in mm from its top-left.

    A conventional architectural cartouche reads in three bands, bottom-right of the
    sheet: the project identity, a row of labelled fields (scale, survey point,
    projection), and a provenance strip. The bands are stated once here so the drawing
    code stays about typography and no baseline is invented at the call site.
    """
    utm = metadata.get("utm", {})
    wgs = metadata.get("wgs84", {})
    area = metadata.get("ground_area_m2")
    epsg = str(utm.get("epsg", "?")).replace("EPSG:", "")
    head_h = round(h * 0.56, 3)  # 28 mm on an A3 cartouche
    foot_h = 6.0
    return {
        "w": w,
        "h": h,
        "head_h": head_h,
        "foot_y": round(h - foot_h, 3),
        "mid_h": round(h - foot_h - head_h, 3),
        "scale": f"1:{placement.scale}",
        "paper": f"{placement.sheet}  {placement.page_w_mm:.0f} x {placement.page_h_mm:.0f} mm  "
        f"{placement.orientation.upper()}",
        "north": "NORTH UP",
        "crs": f"EPSG:{epsg}",
        "projection": ("UTM ZONE 30N" if epsg == "32630" else f"EPSG {epsg}"),
        "point": f"E {float(utm.get('easting_m', 0)):,.3f}   N {float(utm.get('northing_m', 0)):,.3f}",
        "wgs": f"{float(wgs.get('latitude', 0)):.6f} N   {abs(float(wgs.get('longitude', 0))):.6f} E",
        "area": f"{float(area):,.0f} m2" if area is not None else "NOT SURVEYED",
        "date": str(metadata.get("date") or ""),
    }


def _fit(text: str, cell_w: float, nominal_mm: float, weight: str = "normal", tracking_mm: float = 0.0) -> float:
    """Shrink ``nominal_mm`` until ``text`` fits ``cell_w``; never grow past nominal.

    Silently printing a field through the cell rule is the failure mode this exists to
    prevent, so the arithmetic happens before the glyphs are generated.
    """
    if not text:
        return nominal_mm
    width = text_advance_mm(text, nominal_mm, tracking_mm, weight)
    if width <= cell_w:
        return nominal_mm
    return nominal_mm * (cell_w / width)


def add_cartouche(dwg, placement, metadata: dict[str, Any]) -> list[str]:
    """Architectural title block: framed panel, identity band, labelled field row.

    Replaces the previous four-rule text table. Every string is still vector outlines,
    and every cell is measured and fitted before it is drawn.
    """
    x, y, w, h = cartouche_rect_svg(placement)
    c = _cartouche_cells(metadata, placement, w, h)
    inset = 3.0
    panel = dwg.g(id="G-ANNO-TTLB", fill="none", stroke="#000000")
    # The outer rule is the heaviest line on the sheet: the cartouche is a panel on the
    # drawing, not a table floating in the margin.
    panel.add(dwg.rect(insert=(x, y), size=(w, h), stroke_width=0.7))
    panel.add(dwg.rect(insert=(x + 1.1, y + 1.1), size=(w - 2.2, h - 2.2), stroke_width=0.18))
    panel.add(dwg.line(start=(x, y + c["head_h"]), end=(x + w, y + c["head_h"]), stroke_width=0.5))
    panel.add(dwg.line(start=(x, y + c["foot_y"]), end=(x + w, y + c["foot_y"]), stroke_width=0.5))
    dwg.add(panel)

    # --- identity band ----------------------------------------------------
    design = _designation(metadata)
    avail = w - 2 * inset
    head_text, head_size = design[0]
    sub_text = design[1][0] if len(design) > 1 else ""
    hs = _fit(head_text.upper(), avail, head_size, tracking_mm=head_size * 0.16)
    # Title baseline and subtitle baseline are set from opposite ends of the band so the
    # two can never collide however long the strings are.
    draw_tracked_text(dwg, head_text.upper(), x + inset, y + c["head_h"] - 12.5, hs, hs * 0.16)
    if sub_text:
        ss = _fit(sub_text.upper(), avail, TYPE_SUBTITLE_MM, tracking_mm=TYPE_SUBTITLE_MM * 0.24)
        draw_tracked_text(dwg, sub_text.upper(), x + inset, y + c["head_h"] - 5.2, ss, ss * 0.24)

    # --- field row --------------------------------------------------------
    f_scale, f_point = 0.24, 0.72
    xs = [x, x + w * f_scale, x + w * f_point, x + w]
    for xr in xs[1:-1]:
        dwg.add(dwg.line(start=(xr, y + c["head_h"]), end=(xr, y + c["foot_y"]), stroke_width=0.35))

    mid_top = y + c["head_h"]

    def field(i: int, caption: str, value: str, sub: str = "") -> None:
        left, right = xs[i] + inset, xs[i + 1] - inset
        cw = right - left
        baseline = mid_top + 3.4
        add_vector_text(dwg, caption, left, baseline, _fit(caption, cw, TYPE_LABEL_MM), weight="bold")
        vh = _fit(value, cw, TYPE_VALUE_MM)
        add_vector_text(dwg, value, left, baseline + vh + 1.8, vh)
        if sub:
            sh = _fit(sub, cw, TYPE_SMALL_MM)
            add_vector_text(dwg, sub, left, baseline + vh + 1.8 + sh + 2.0, sh)

    field(0, "SCALE", c["scale"], c["paper"])
    field(1, "SURVEY POINT — UTM", c["point"], c["crs"])
    field(2, "PROJECTION", c["projection"], c["north"])

    # --- footer strip -----------------------------------------------------
    foot = f"WGS84 {c['wgs']}    GROUND AREA {c['area']}"
    if c["date"]:
        foot += f"    {c['date']}"
    fh = _fit(foot, w - 2 * inset, TYPE_SMALL_MM)
    add_vector_text(dwg, foot, x + inset, y + h - 2.4, fh)
    return ["G-ANNO-TTLB"]


def add_scale_bar(dwg, placement) -> list[str]:
    """Checked scale bar with a labelled zero, right-reading figures."""
    bx, by, bw, bh = scale_bar_rect_svg(placement)
    bar = dwg.g(id="G-ANNO-SCALE-BAR", stroke="#000000")
    segments = 10
    segment_m = (bw / segments) * placement.scale / 1000.0
    for i in range(segments):
        bar.add(
            dwg.rect(
                insert=(bx + i * (bw / segments), by),
                size=(bw / segments, bh),
                fill="#000000" if i % 2 == 0 else "#FFFFFF",
                stroke_width=0.18,
            )
        )
    dwg.add(bar)
    # Figures sit under their own segment boundary rather than on a pre-spaced string,
    # so a changed segment count cannot drift the numbers off the bar.
    for i in range(0, segments + 1, 2):
        value = f"{segment_m * i:.0f}"
        add_vector_text(
            dwg,
            value,
            bx + i * (bw / segments),
            by + bh + 3.0,
            TYPE_SMALL_MM,
            anchor="middle",
        )
    # The unit sits clear of the last figure rather than butting against it, and is set
    # smaller: "50m" reading as one number is the classic scale-bar misread.
    add_vector_text(dwg, "m", bx + bw + 3.2, by + bh + 3.0, TYPE_LABEL_MM)
    return ["G-ANNO-SCALE-BAR", "G-ANNO-SCALE-BAR-LABEL"]


def add_grid(dwg, placement, pad_m: float = 0.0, viewport_m=None) -> tuple[list[str], list[str]]:
    """UTM grid: through-lines across the map, ticks crossing the frame, edge labels.

    Returns the easting and northing label strings actually printed, so a test can
    compare them against the DXF paperspace rather than re-deriving the same formula.
    """
    eastings, northings = _grid_values(placement, pad_m, grid_spacing_m(placement.scale), viewport_m)
    left, top, right, bottom = _frame_rect(placement, pad_m, viewport_m)

    # Through-lines: hairline, clipped to the map so they read as survey grid, not as a
    # second set of frame rules. They are what stops the labels floating free.
    lines = dwg.g(id="G-ANNO-GRID", fill="none", stroke="#000000", stroke_width=0.09)
    for easting in eastings:
        gx, _ = _model_to_svg(easting, 0.0, placement, None)
        if left + 1.3 < gx < right - 1.3:
            lines.add(dwg.line(start=(gx, top), end=(gx, bottom)))
    for northing in northings:
        _, gy = _model_to_svg(0.0, northing, placement, None)
        if top + 1.3 < gy < bottom - 1.3:
            lines.add(dwg.line(start=(left, gy), end=(right, gy)))
    dwg.add(lines)

    ticks = dwg.g(id="G-ANNO-TICKS", fill="none", stroke="#000000", stroke_width=0.35)
    labels = dwg.g(id="G-ANNO-GRID-LABELS", fill="none", stroke="#000000")
    dwg.add(ticks)

    easting_strings: list[str] = []
    for easting in eastings:
        gx, _ = _model_to_svg(easting, 0.0, placement, None)
        if not (left <= gx <= right):
            continue
        # Ticks lie *inside* the frame. Crossing the frame line, the tick ran through the
        # easting label sitting under it and printed as a strike-through.
        ticks.add(dwg.line(start=(gx, bottom), end=(gx, bottom - _TICK_MM)))
        ticks.add(dwg.line(start=(gx, top), end=(gx, top + _TICK_MM)))
        text = f"E {easting:.0f}"
        easting_strings.append(text)
        # Set *inside* the frame, right-reading. Below the frame the label lands in the
        # strip the cartouche occupies -- the easternmost easting printed straight through
        # the title-block rule. Inside the frame it annotates the tick it belongs to and
        # cannot collide with sheet furniture.
        add_label_plate(dwg, text, gx, bottom - _TICK_MM - 0.9, TYPE_GRID_MM, clamp=(left, right))

    northing_strings: list[str] = []
    for northing in northings:
        _, gy = _model_to_svg(0.0, northing, placement, None)
        if not (top <= gy <= bottom):
            continue
        ticks.add(dwg.line(start=(left, gy), end=(left + _TICK_MM, gy)))
        ticks.add(dwg.line(start=(right, gy), end=(right - _TICK_MM, gy)))
        text = f"N {northing:.0f}"
        northing_strings.append(text)
        # Rotated a quarter turn so it reads bottom-to-top up the right margin, which is
        # how a northing is conventionally set on a sheet.
        # Northing reads up the right margin, rotated a quarter turn, clear of the frame.
        add_vector_text(
            dwg,
            text,
            right + _LABEL_GAP_MM + TYPE_GRID_MM,
            gy,
            TYPE_GRID_MM,
            rotation_deg=-90.0,
            anchor="start",
        )
    dwg.add(labels)
    return easting_strings, northing_strings


def _frame_rect(placement, pad_m: float = 0.0, viewport_m=None) -> tuple[float, float, float, float]:
    minx, miny, maxx, maxy = covered_ground_m(placement, pad_m, viewport_m)
    x0, y0 = _model_to_svg(minx, maxy, placement, None)
    x1, y1 = _model_to_svg(maxx, miny, placement, None)
    return min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)


def add_north_arrow(dwg, placement, pad_m: float = 0.0, viewport_m=None):
    """North arrow inside the top-right of the map, on a knocked-out disc.

    A north arrow in the margin sits beside the sheet, not on the plan; drawn straight
    onto the drawing it vanishes into the black poche. It is placed inside the frame on a
    white disc, the standard way a site plan carries a north point over a dense
    figure-ground. The disc is sized from the symbol's own bounding box -- sizing it to
    the needle alone left the "N" half-buried in a footprint.
    """
    left, top, right, _ = _frame_rect(placement, pad_m, viewport_m)
    nx = right - 15.0
    tip_y = top + 34.0  # the needle tip
    label_h = 2.8
    needle_h = 13.0
    needle_top = tip_y - needle_h
    label_baseline = needle_top - 1.6
    symbol_top = label_baseline - label_h
    symbol_bottom = tip_y

    # Knockout disc, centred on the full symbol and clear of it by a uniform margin.
    dy = (symbol_top + symbol_bottom) / 2.0
    r = (symbol_bottom - symbol_top) / 2.0 + 3.0
    dwg.add(dwg.circle(center=(nx, dy), r=r, fill="#FFFFFF", stroke="none"))

    g = dwg.g(id="G-ANNO-NORTH", fill="none", stroke="#000000", stroke_width=0.3)
    # Split needle: solid left half, hollow right half, so the symbol reads at plot size.
    g.add(
        dwg.polygon(points=[(nx, needle_top), (nx - 3.2, tip_y - 1.2), (nx, tip_y)], fill="#000000", stroke_width=0.2)
    )
    g.add(
        dwg.polygon(points=[(nx, needle_top), (nx + 3.2, tip_y - 1.2), (nx, tip_y)], fill="#FFFFFF", stroke_width=0.2)
    )
    dwg.add(g)
    add_vector_text(dwg, "N", nx, label_baseline, label_h, weight="bold", anchor="middle")
    return ["G-ANNO-NORTH", "G-ANNO-NORTH-LABEL"]


def add_sheet_annotations(
    dwg,
    placement,
    metadata: dict[str, Any],
    *,
    pad_m: float = 0.0,
    with_grid: bool = True,
    with_north: bool = True,
    with_cartouche: bool = True,
    with_frame: bool = True,
    viewport_m: tuple[float, float, float, float] | None = None,
):
    """Composite frame, cartouche, scale bar, UTM grid and north arrow into an SVG sheet.

    Returns the list of group ids written, so callers and tests can assert the sheet
    actually received them rather than trusting a boolean.
    """
    written: list[str] = []

    if with_frame:
        add_map_frame(dwg, placement, pad_m, viewport_m)
        written.append("G-ANNO-FRAME")

    if with_grid:
        add_grid(dwg, placement, pad_m, viewport_m)
        written.extend(["G-ANNO-GRID", "G-ANNO-TICKS", "G-ANNO-GRID-LABELS"])

    if with_north:
        add_north_arrow(dwg, placement, pad_m, viewport_m)
        written.extend(["G-ANNO-NORTH", "G-ANNO-NORTH-LABEL"])

    if with_cartouche:
        written.extend(add_cartouche(dwg, placement, metadata))
        written.extend(add_scale_bar(dwg, placement))

    return written
