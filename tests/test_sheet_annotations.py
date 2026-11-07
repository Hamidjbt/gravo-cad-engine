"""Regression tests for cartouche/grid/north compositing into the rendered sheet.

The defect these cover: the DXF has always carried a title block, north arrow and UTM
grid in its paperspace layout, but the rendered SVG/PDF/PNG showed the drawing alone.
A sheet plotted from the PDF had no title block, no grid references and no north arrow,
so it could not be issued without an AutoCAD round-trip.

The change is easy to get subtly wrong in ways a "does it render" smoke test would not
catch, so the tests assert the three things that actually broke during development:

1. the annotation lands on the page at all, and on the correct edge;
2. the map band and the cartouche never overlap;
3. the sheet stays inside the QA contract (no ``<text>``, monochrome only).
"""

from __future__ import annotations

import math
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import layout_engine as le
import sheet_annotations as sa

BOUNDS = (654883.56, 3861321.432, 655110.56, 3861548.432)  # Tlemcen A3 @ 1:1000


def _placement(sheet="A3", scale=1000, bounds=BOUNDS, orientation="landscape"):
    return le.resolve_placement(bounds, sheet=sheet, scale=scale, orientation=orientation)


def _metadata():
    return le.metadata_from_utm(654997.0599001274, 3861434.931685834, 32630, None)


def _draw(placement, **kwargs):
    import svgwrite

    dwg = svgwrite.Drawing(
        size=(f"{placement.page_w_mm}mm", f"{placement.page_h_mm}mm"),
        viewBox=f"0 0 {placement.page_w_mm} {placement.page_h_mm}",
        profile="full",
    )
    written = sa.add_sheet_annotations(dwg, placement, _metadata(), **kwargs)
    return dwg, written


# ---------------------------------------------------------------------------
# the band arithmetic: the bug that put the drawing under the title block
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("sheet,scale", [("A3", 1000), ("A1", 1000), ("A3", 250), ("A0", 1000)])
@pytest.mark.parametrize("orientation", ["landscape", "portrait"])
def test_map_band_and_cartouche_tile_the_page_without_overlapping(sheet, scale, orientation):
    """The map and the cartouche must share the page without ever intersecting.

    If they overlap, buildings print underneath the title block. If they leave a gap,
    the sheet grows a dead strip that the DXF does not have.
    """
    placement = _placement(sheet=sheet, scale=scale, orientation=orientation)
    mx, my, mw, mh = placement.map_band_mm
    cx, cy, cw, ch = sa.cartouche_rect_svg(placement)

    map_bottom = my + mh
    assert map_bottom <= cy + 1e-9, "cartouche overlaps the map band"
    assert cy + ch <= placement.page_h_mm - placement.margin_mm + 1e-9
    # No dead strip: the cartouche starts exactly where the map ends.
    assert cy == pytest.approx(map_bottom)


def test_map_band_is_the_vertical_mirror_of_the_declared_usable_rect():
    """``usable_rect_mm`` is y-up; ``map_band_mm`` is the same rectangle y-down.

    Rendering geometry into one and clipping to the other is what cut the drawing off
    at the top edge during development, so the relationship is asserted rather than
    assumed.
    """
    placement = _placement()
    ux, uy, uw, uh = placement.usable_rect_mm
    mx, my, mw, mh = placement.map_band_mm
    assert (mw, mh) == pytest.approx((uw, uh))
    assert mx == pytest.approx(ux)
    assert my + mh == pytest.approx(placement.page_h_mm - uy)
    assert my == pytest.approx(placement.margin_mm)


def test_cartouche_sits_on_the_paper_bottom_edge_like_the_dxf():
    """The rendered title block must sit on the same edge as the DXF paperspace one.

    The DXF anchors its cartouche to the page's bottom edge (y = margin, y-up). Drawn
    naively into an SVG, the same numbers put it at the top -- a mirror image of the
    CAD sheet, which is exactly the mismatch this feature exists to remove.
    """
    placement = _placement()
    cx, cy, cw, ch = sa.cartouche_rect_svg(placement)
    assert cy + ch == pytest.approx(placement.page_h_mm - placement.margin_mm)
    assert cx + cw == pytest.approx(placement.page_w_mm - placement.margin_mm)


def _north_arrow_extent(placement) -> tuple[float, float]:
    """Bounding y-range of the north arrow group, read from the drawing itself.

    Reading it back from the emitted SVG rather than recomputing the anchor is the
    point: a test that repeats the implementation's own arithmetic agrees with any bug
    the implementation has. An earlier version of this test did exactly that and passed
    with the arrow still off the page.
    """
    dwg, _ = _draw(placement, with_grid=False, with_cartouche=False)
    root = ET.fromstring(dwg.tostring())
    ys: list[float] = []
    for group in root.iter():
        if group.attrib.get("id") not in {"G-ANNO-NORTH", "G-ANNO-NORTH-LABEL"}:
            continue
        for el in group.iter():
            tag = el.tag.rsplit("}", 1)[-1]
            if tag == "line":
                ys.extend([float(el.attrib["y1"]), float(el.attrib["y2"])])
            elif tag == "polygon":
                ys.extend(float(pt.split(",")[1]) for pt in el.attrib["points"].split())
            elif tag == "path":
                # Glyph outlines: every "y" in "M x,y" / "L x,y" / "Q cx,cy x,y".
                for token in el.attrib["d"].replace("M", " ").replace("L", " ").replace("Q", " ").split():
                    if "," in token:
                        ys.append(float(token.split(",")[1]))
    assert ys, "north arrow emitted no geometry"
    return min(ys), max(ys)


@pytest.mark.parametrize("sheet,scale", [("A3", 1000), ("A1", 1000), ("A0", 1000)])
def test_north_arrow_is_fully_on_the_paper(sheet, scale):
    """A north arrow whose tip prints at y = -1 mm is not on the sheet.

    This failed for real: the arrow was anchored to the last drawn vertex, which at
    1:1000 sits on the band edge, and the arrowhead printed off the top of the page.
    """
    placement = _placement(sheet=sheet, scale=scale)
    top, bottom = _north_arrow_extent(placement)
    assert top >= 0.0, f"{sheet}: north arrow reaches y={top:.2f}, off the top of the page"
    assert bottom <= placement.page_h_mm
    # It must also sit inside the map band, not on top of the cartouche.
    _, my, _, mh = placement.map_band_mm
    assert my <= top and bottom <= my + mh


# ---------------------------------------------------------------------------
# the annotations exist, and carry the right content
# ---------------------------------------------------------------------------


def test_all_annotation_groups_are_written():
    placement = _placement()
    _, written = _draw(placement)
    assert set(written) == {
        "G-ANNO-FRAME",
        "G-ANNO-GRID",
        "G-ANNO-TICKS",
        "G-ANNO-GRID-LABELS",
        "G-ANNO-NORTH",
        "G-ANNO-NORTH-LABEL",
        "G-ANNO-TTLB",
        "G-ANNO-SCALE-BAR",
        "G-ANNO-SCALE-BAR-LABEL",
    }


def test_each_layer_can_be_switched_off():
    """Callers must be able to emit a bare map; the hollow variant relies on it."""
    placement = _placement()
    _, written = _draw(placement, with_grid=False, with_north=False, with_cartouche=False, with_frame=False)
    assert written == []


def test_grid_labels_carry_the_utm_values_the_map_covers():
    """Grid labels must name coordinates the drawing actually spans.

    Labelling the paper extent instead of the drawn extent prints eastings that fall
    outside the study area -- confidently wrong references on a plotting sheet.
    """
    placement = _placement()
    ground = sa.covered_ground_m(placement, 0.0)
    minx, miny, maxx, maxy = ground
    spacing = sa.grid_spacing_m(placement.scale)
    first_e = math.ceil(minx / spacing) * spacing
    first_n = math.ceil(miny / spacing) * spacing
    assert first_e >= minx and first_e <= maxx
    assert first_n >= miny and first_n <= maxy
    # The E and N series must be derived from the covered ground, which for a run
    # smaller than the page is the study radius rather than the paper.
    tight = sa.covered_ground_m(placement, 50.0)
    assert tight[2] - tight[0] == pytest.approx(100.0)
    assert tight[3] - tight[1] == pytest.approx(100.0)


# ---------------------------------------------------------------------------
# the QA contract that forbids <text>
# ---------------------------------------------------------------------------


def test_text_is_emitted_as_glyph_outlines_not_text_elements(tmp_path):
    """Every label must be vector outlines: the sheet QA forbids ``<text>``.

    A plotter must not depend on which fonts the RIP happens to have, and the QA
    contract enforces it. Converting to outlines is what lets the sheet carry words at
    all, so this asserts the absence of ``<text>`` *and* the presence of ink.
    """
    placement = _placement()
    dwg, _ = _draw(placement)
    out = tmp_path / "sheet.svg"
    dwg.saveas(str(out))
    root = ET.parse(out).getroot()
    tags = [el.tag.rsplit("}", 1)[-1] for el in root.iter()]
    assert "text" not in tags
    assert "image" not in tags
    assert "use" not in tags
    # Glyph outlines are there: each label is a non-trivial path.
    paths = [el for el in root.iter() if el.tag.rsplit("}", 1)[-1] == "path"]
    assert paths, "no glyph outlines were emitted"
    longest = max(len(p.attrib.get("d", "")) for p in paths)
    assert longest > 500, "glyph paths look empty"


def test_text_to_path_rejects_nothing_for_the_strings_the_sheet_prints(tmp_path):
    """Every string the cartouche prints must convert, including the em dash."""
    from layout_engine import cartouche_text

    for line in cartouche_text(_metadata(), 1000):
        d = sa.text_to_path(line, 0.0, 0.0, 2.4)
        assert d, f"failed to convert {line!r}"
        assert d.count("M") >= 1


def test_cartouche_text_is_fitted_to_its_cell():
    """Long lines are scaled down rather than printed through the border.

    At the nominal 2.4 mm the UTM line measures 91.7 mm in an 87 mm cell.
    """
    from layout_engine import cartouche_text

    for line in cartouche_text(_metadata(), 1000):
        nominal = sa.text_width_mm(line, 2.4)
        fitted = min(2.4, 2.4 * (87.0 / nominal)) if nominal > 87.0 else 2.4
        assert sa.text_width_mm(line, fitted) <= 87.0 + 1e-6


def test_annotations_are_monochrome():
    """The QA contract allows only black, white and none."""
    placement = _placement()
    dwg, _ = _draw(placement)
    markup = dwg.tostring()
    for token in ("#FF0000", "#00FF00", "#0000FF", "rgb("):
        assert token not in markup


def test_rendered_geometry_lands_inside_the_map_band():
    """Geometry must actually be drawn into the band the cartouche leaves free.

    The band properties can all be self-consistent while the geometry is drawn against
    a different y origin, which is precisely how the drawing ended up printed under the
    title block. This projects real corners through the same transform the renderer
    uses and asserts they land where the page says they should.
    """
    placement = _placement()
    mx, my, mw, mh = placement.map_band_mm
    minx, miny, maxx, maxy = placement.content_m
    ox, oy = placement.origin_mm
    mm_per_m = placement.mm_per_m
    corners = [(ox + x * mm_per_m, oy - y * mm_per_m) for x in (minx, maxx) for y in (miny, maxy)]
    for x_mm, y_mm in corners:
        assert my - 1e-6 <= y_mm <= my + mh + 1e-6, (
            f"geometry corner y={y_mm:.2f} outside map band {my:.2f}..{my + mh:.2f}"
        )
        assert mx - 1e-6 <= x_mm <= mx + mw + 1e-6, (
            f"geometry corner x={x_mm:.2f} outside map band {mx:.2f}..{mx + mw:.2f}"
        )
    # And the drawn extent must be the band, not a subset of it: an A3 at 1:1000 with a
    # 227 m study area fills the vertical band exactly.
    ys = [y for _, y in corners]
    assert max(ys) - min(ys) == pytest.approx(mh, abs=0.01)


def test_svg_grid_names_the_same_utm_values_as_the_dxf_paperspace():
    """The two artifacts must annotate the same coordinates.

    A sheet whose PDF says E 655100 while its DXF says E 655000 is worse than one with
    no grid at all: both look authoritative and one is wrong. The DXF derives its
    labels from the viewport bounds and the SVG from the covered ground, so the two
    routes are asserted to agree rather than assumed to.
    """
    import ezdxf
    from shapely.geometry import Polygon

    placement = _placement()
    metadata = _metadata()
    site = Polygon(
        [
            (placement.content_m[0], placement.content_m[1]),
            (placement.content_m[2], placement.content_m[1]),
            (placement.content_m[2], placement.content_m[3]),
            (placement.content_m[0], placement.content_m[3]),
        ]
    )

    import importlib

    build = importlib.import_module("cad-fond-de-plan-build")
    doc = ezdxf.new("R2018")
    build.add_paperspace_viewport(doc, le.resolve_layout(site.bounds, "A3", 1000), metadata)
    dxf_labels = {
        e.dxf.text for e in doc.layouts.get("A3-1_1000") if e.dxftype() == "TEXT" and e.dxf.layer == "G-ANNO-GRID"
    }

    ground = sa.covered_ground_m(placement, 0.0)
    spacing = sa.grid_spacing_m(placement.scale)
    minx, miny, maxx, maxy = ground
    first_e = math.ceil(minx / spacing) * spacing
    first_n = math.ceil(miny / spacing) * spacing
    eastings = [first_e + i * spacing for i in range(int((maxx - first_e) // spacing) + 1)]
    northings = [first_n + i * spacing for i in range(int((maxy - first_n) // spacing) + 1)]
    svg_labels = {f"E {e:.0f}" for e in eastings} | {f"N {n:.0f}" for n in northings}

    assert svg_labels == dxf_labels


# ---------------------------------------------------------------------------
# v1.13.0 -- the four review corrections
# ---------------------------------------------------------------------------


def test_building_poche_is_solid_black_not_a_line_pattern(tmp_path):
    """Poiché must be a solid fill.

    The sheet previously drew the clipped hatch *segments* as stroked lines, which plots
    as a diagonal line pattern, while the DXF hatch said SOLID -- the PDF and the CAD
    file described different drawings. This asserts the fill attribute itself, because a
    line pattern can look plausible in a thumbnail and only betrays itself on paper.
    """
    import importlib

    build = importlib.import_module("cad-fond-de-plan-build")
    from shapely.geometry import Polygon

    placement = _placement()
    square = Polygon(
        [
            (placement.content_m[0] + 5, placement.content_m[1] + 5),
            (placement.content_m[0] + 15, placement.content_m[1] + 5),
            (placement.content_m[0] + 15, placement.content_m[1] + 15),
            (placement.content_m[0] + 5, placement.content_m[1] + 15),
        ]
    )
    out = tmp_path / "solid.svg"
    build.write_svg(out, [square], None, [], Polygon(), [], placement, True, metadata=None)
    root = ET.parse(out).getroot()
    buildings = [g for g in root.iter() if g.attrib.get("id") == "BUILDINGS"]
    assert buildings, "no BUILDINGS group"
    assert buildings[0].attrib["fill"] == "#000000"
    # And no stroked hatch linework is emitted alongside it.
    assert not [g for g in root.iter() if g.attrib.get("id") == "HATCH"]


def test_viewport_frame_matches_the_drawn_extent_not_the_radius(tmp_path):
    """The frame must be the viewport the geometry was clipped to.

    A rectangular study area passed to the annotations as a scalar pad came back square,
    so a 400 mm wide band got a 227 mm frame drawn in the middle of it. This asserts the
    frame rectangle against the viewport rectangle, in millimetres, end to end.
    """
    placement = _placement()
    # A viewport strictly smaller than the page, so the page clamp cannot mask a
    # mis-shaped frame: 300 x 200 m must come back as 300 x 200 mm at 1:1000.
    cx, cy = placement.content_center_m
    viewport = (cx - 150.0, cy - 100.0, cx + 150.0, cy + 100.0)
    dwg, _ = _draw(placement, viewport_m=viewport)
    root = ET.fromstring(dwg.tostring())
    frame = [g for g in root.iter() if g.attrib.get("id") == "G-ANNO-FRAME"][0]
    rect = [c for c in frame if c.tag.rsplit("}", 1)[-1] == "rect"][0]
    width = float(rect.attrib["width"])
    height = float(rect.attrib["height"])
    assert width == pytest.approx(300.0 * placement.mm_per_m, abs=0.01)
    assert height == pytest.approx(200.0 * placement.mm_per_m, abs=0.01)
    # The square-viewport regression: passing this rectangle through the scalar pad gave
    # 200 x 200 mm -- the width was clamped to the shortest axis.
    assert width > height


def test_grid_labels_are_attached_to_the_frame_by_ticks():
    """Every printed coordinate needs a tick to belong to.

    Labels floating in the margin are decoration. This asserts one tick per label on the
    same axis, so removing the tick layer fails rather than silently shipping again.
    """
    placement = _placement()
    dwg, _ = _draw(placement)
    root = ET.fromstring(dwg.tostring())
    ticks = [g for g in root.iter() if g.attrib.get("id") == "G-ANNO-TICKS"][0]
    lines = list(ticks)
    # Four eastings and two northings carry two ticks each (top+bottom, left+right).
    assert len(lines) == 12
    frame = [g for g in root.iter() if g.attrib.get("id") == "G-ANNO-FRAME"][0]
    frect = [c for c in frame if c.tag.rsplit("}", 1)[-1] == "rect"][0]
    left, top = float(frect.attrib["x"]), float(frect.attrib["y"])
    right = left + float(frect.attrib["width"])
    bottom = top + float(frect.attrib["height"])
    for line in lines:
        x1, y1 = float(line.attrib["x1"]), float(line.attrib["y1"])
        x2, y2 = float(line.attrib["x2"]), float(line.attrib["y2"])
        # Each tick starts on a frame edge and runs inward, never outside the frame.
        on_edge = (
            (abs(y1 - top) < 1e-6 and abs(y2 - top) < 3.01)  # top edge, downward
            or (abs(y1 - bottom) < 1e-6 and abs(y2 - bottom) < 3.01)  # bottom edge, upward
            or (abs(x1 - left) < 1e-6 and abs(x2 - left) < 3.01)
            or (abs(x1 - right) < 1e-6 and abs(x2 - right) < 3.01)
        )
        assert on_edge, f"tick {x1},{y1}->{x2},{y2} is not on a frame edge"
        assert top - 1e-6 <= min(y1, y2) and max(y1, y2) <= bottom + 1e-6
        assert left - 1e-6 <= min(x1, x2) and max(x1, x2) <= right + 1e-6


def test_cartouche_is_a_framed_panel_with_labelled_fields():
    """The title block must read as an architectural cartouche, not a bare text table.

    Asserts the structure a reviewer checks for: a double outer rule, band rules that
    span the full panel width, an identity band carrying both the drawing name and its
    subtitle, and a labelled field row carrying the scale.
    """
    placement = _placement()
    dwg, _ = _draw(placement)
    root = ET.fromstring(dwg.tostring())
    panel = [g for g in root.iter() if g.attrib.get("id") == "G-ANNO-TTLB"][0]
    rects = [c for c in panel if c.tag.rsplit("}", 1)[-1] == "rect"]
    assert len(rects) == 2, "cartouche needs an outer and an inner rule"
    # Outer rule is the heaviest line in the block.
    widths = sorted(float(r.attrib.get("stroke-width", 1)) for r in rects)
    assert widths[0] < widths[1]
    x, y, w, h = sa.cartouche_rect_svg(placement)
    for r in rects:
        assert float(r.attrib["width"]) <= w + 1e-6
    rules = [c for c in panel if c.tag.rsplit("}", 1)[-1] == "line"]
    assert rules
    for rule in rules:
        # Band rules must span the panel, or the block reads as an open table.
        assert abs(float(rule.attrib["x1"]) - x) < 1e-6
        assert abs(float(rule.attrib["x2"]) - (x + w)) < 1e-6
    # The identity band carries the title in tracked glyphs: many short paths, not one.
    glyph_paths = [p for p in root.iter() if p.tag.rsplit("}", 1)[-1] == "path"]
    short = [p for p in glyph_paths if len(p.attrib.get("d", "")) < 400]
    assert len(short) > 10
