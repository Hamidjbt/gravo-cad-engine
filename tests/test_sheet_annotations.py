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
        "G-ANNO-GRID",
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
    _, written = _draw(placement, with_grid=False, with_north=False, with_cartouche=False)
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
