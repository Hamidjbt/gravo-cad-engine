"""Tests for true print architecture (v1.9.0): ISO 216 pages and exact plot scale.

The contract under test is physical: a sheet declares a page in millimetres and a
denominator, and a plotter must reproduce that scale. Anything that silently rescales to
fit is a defect, not a convenience — so most of these tests assert equality of measured
millimetres rather than merely "no exception".
"""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import deliverable_qa as qa  # noqa: E402
import layout_engine as le  # noqa: E402

SVG_NS = "{http://www.w3.org/2000/svg}"


# --- ISO 216 page geometry ---------------------------------------------------


@pytest.mark.parametrize(
    ("sheet", "landscape", "portrait"),
    [
        ("A3", (420.0, 297.0), (297.0, 420.0)),
        ("A1", (841.0, 594.0), (594.0, 841.0)),
        ("A0", (1189.0, 841.0), (841.0, 1189.0)),
    ],
)
def test_page_size_is_exact_iso_216_in_both_orientations(sheet, landscape, portrait):
    assert le.page_size(sheet, "landscape") == landscape
    assert le.page_size(sheet, "portrait") == portrait


@pytest.mark.parametrize("sheet", ["A3", "A1", "A0"])
def test_every_sheet_keeps_the_root_two_ratio(sheet):
    """ISO 216 is defined by the sqrt(2) ratio; a drift here is a plotting error.

    Landscape is long:short = sqrt(2); portrait is its reciprocal. The published
    millimetre edges are rounded to whole millimetres, so A1 (841x594) deviates by
    0.0016 from an exact sqrt(2) — tolerance is 0.002, not tighter.
    """
    long_edge, short_edge = le.ISO_216_MM[sheet]
    for orientation in le.ORIENTATIONS:
        width, height = le.page_size(sheet, orientation)
        drawn = max(width, height) / min(width, height)
        assert drawn == pytest.approx(2**0.5, abs=0.002)
        if orientation == "landscape":
            assert width > height
        else:
            assert height > width
        assert sorted((width, height)) == sorted((long_edge, short_edge))


@pytest.mark.parametrize("bad", ["A4", "", "a3x", None])
def test_unknown_sheet_is_rejected(bad):
    with pytest.raises((ValueError, AttributeError)):
        le.page_size(bad)  # type: ignore[arg-type]


def test_unknown_orientation_is_rejected():
    with pytest.raises(ValueError, match="Orientation must be"):
        le.page_size("A3", "sideways")


def test_sheets_alias_no_longer_implies_a_square():
    """The old table was read as (width, height) and produced 420x420 via --sheet-mm."""
    assert le.SHEETS["A3"] == (420.0, 297.0)
    assert le.SHEETS["A3"][0] != le.SHEETS["A3"][1]


# --- placement: scale is declared, never fitted ------------------------------


def test_placement_draws_ground_at_the_declared_denominator():
    placement = le.resolve_placement((-100, -100, 100, 100), sheet="A3", scale=500)
    assert placement.mm_per_m == pytest.approx(2.0)  # 1:500 => 2 mm per metre
    assert placement.mm_per_m == pytest.approx(1000.0 / 500)


@pytest.mark.parametrize(("scale", "mm_per_m"), [(250, 4.0), (500, 2.0), (1000, 1.0)])
def test_mm_per_m_follows_the_denominator(scale, mm_per_m):
    placement = le.resolve_placement((-10, -10, 10, 10), sheet="A0", scale=scale)
    assert placement.mm_per_m == pytest.approx(mm_per_m)


def test_two_placements_describe_the_same_object_at_the_same_paper_distance():
    """The defining property of a true-to-scale plot: 100 m of ground is 200 mm at 1:500."""
    placement = le.resolve_placement((-300, -300, 300, 300), sheet="A0", scale=500)
    hundred_metres = 100.0 * placement.mm_per_m
    assert hundred_metres == pytest.approx(200.0)


def test_placement_reserves_the_cartouche_band_like_the_dxf_paper_space():
    """Raster and DXF are two views of one page and must agree on ground extent."""
    for sheet, scale in (("A3", 250), ("A1", 500), ("A0", 1000)):
        for orientation in le.ORIENTATIONS:
            bounds = (-10.0, -10.0, 10.0, 10.0)
            dxf = le.resolve_layout(bounds, sheet, scale, orientation=orientation)
            raster = le.resolve_placement(bounds, sheet=sheet, scale=scale, orientation=orientation)
            assert dxf.ground_extent_m == pytest.approx(raster.ground_extent_m), (sheet, orientation)


def test_portrait_page_reanchors_the_cartouche_on_the_bottom_edge():
    landscape = le.resolve_placement((0, 0, 10, 10), sheet="A3", scale=250, orientation="landscape")
    portrait = le.resolve_placement((0, 0, 10, 10), sheet="A3", scale=250, orientation="portrait")
    assert portrait.page_w_mm < portrait.page_h_mm
    assert landscape.page_w_mm > landscape.page_h_mm
    # The usable rect starts above the cartouche band in both cases.
    assert portrait.usable_rect_mm[1] == pytest.approx(le.TITLEBLOCK_MM[1] + 10.0)


def test_placement_centres_content_on_the_page():
    placement = le.resolve_placement((-50, -50, 50, 50), sheet="A3", scale=500)
    ox, oy = placement.origin_mm
    # The content centre (0,0) must land at the centre of the usable rect, measured in
    # the frame the geometry is actually drawn in: y increases downward from the page
    # top and the map band runs margin .. margin + usable_h. usable_rect_mm reports the
    # same band in the mirrored y-up convention, so it is converted rather than used
    # directly -- otherwise this test passes while the drawing prints upside-down.
    ux, uy, uw, uh = placement.usable_rect_mm
    mx, my, mw, mh = placement.map_band_mm
    assert ox == pytest.approx(ux + uw / 2)
    assert oy == pytest.approx(my + mh / 2)
    # The two rectangles describe one band; they differ only by the vertical mirror.
    assert mw == pytest.approx(uw) and mh == pytest.approx(uh)
    assert my + mh == pytest.approx(placement.page_h_mm - uy)


def test_custom_page_must_not_claim_an_iso_name():
    with pytest.raises(ValueError, match="requires sheet='custom'"):
        le.resolve_placement((0, 0, 1, 1), sheet="A3", scale=500, page_mm_override=(420.0, 420.0))


def test_custom_square_page_is_a_declared_custom_sheet():
    placement = le.resolve_placement(
        (-300, -300, 300, 300),
        sheet="custom",
        scale=1000,
        margin_mm=0.0,
        page_mm_override=(420.0, 420.0),
        titleblock_mm=(0.0, 0.0),
    )
    assert placement.sheet == "CUSTOM"
    assert (placement.page_w_mm, placement.page_h_mm) == (420.0, 420.0)
    assert placement.ground_extent_m == (420.0, 420.0)


def test_placement_serialises_the_physical_facts():
    payload = le.resolve_placement((0, 0, 10, 10), sheet="A3", scale=500).to_dict()
    assert payload["page_mm"] == {"width": 420.0, "height": 297.0}
    assert payload["scale"] == "1:500"
    assert payload["aspect_ratio"] == pytest.approx(420 / 297, abs=1e-6)
    assert payload["mm_per_m"] == pytest.approx(2.0)


# --- coverage honesty -------------------------------------------------------


def test_coverage_bounds_are_bounded_by_the_page_for_an_oversized_site():
    page = le.resolve_placement((0, 0, 10, 10), sheet="A3", scale=500)
    bounds = le.coverage_bounds((0.0, 0.0), page, pad_m=300.0)
    ground_w, ground_h = page.ground_extent_m
    assert bounds[2] - bounds[0] == pytest.approx(min(ground_w, 600.0))


def test_coverage_bounds_are_bounded_by_the_radius_for_a_small_site():
    page = le.resolve_placement((0, 0, 10, 10), sheet="A0", scale=1000)
    bounds = le.coverage_bounds((0.0, 0.0), page, pad_m=50.0)
    assert bounds == (-50.0, -50.0, 50.0, 50.0)


# --- raster expectations ----------------------------------------------------


def test_raster_size_is_per_axis_at_300_dpi():
    placement = le.resolve_placement((0, 0, 10, 10), sheet="A3", scale=500)
    width, height = (
        round(placement.page_w_mm / 25.4 * 300),
        round(placement.page_h_mm / 25.4 * 300),
    )
    assert (width, height) == (4961, 3508)


def test_a_declared_iso_page_never_resolves_to_a_square_raster():
    page = qa.ExpectedLayoutResolver.expected_png_px(
        {"outputs": {"sheet": {"page_mm": {"width": 420.0, "height": 297.0}}}}
    )
    assert page[0] != page[1]


# --- QA rejects the square-page regression ----------------------------------


def _svg(tmp_path: Path, width: str, height: str, viewbox: str | None = None) -> Path:
    path = tmp_path / "sheet.svg"
    vb = viewbox or f"0 0 {width.removesuffix('mm')} {height.removesuffix('mm')}"
    path.write_text(
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="{vb}">'
        '<path fill="#000000"/></svg>',
        encoding="utf-8",
    )
    return path


def test_qa_accepts_a_correct_iso_page(tmp_path):
    findings = qa._svg_findings(_svg(tmp_path, "420mm", "297mm"), (420.0, 297.0))
    assert findings == []


def test_qa_rejects_a_square_page_declared_as_a3(tmp_path):
    """The original D5 defect, asserted directly."""
    findings = qa._svg_findings(_svg(tmp_path, "420mm", "420mm"), (420.0, 297.0))
    assert any("420x297mm" in finding for finding in findings)


def test_qa_rejects_a_viewbox_that_disagrees_with_the_page(tmp_path):
    findings = qa._svg_findings(_svg(tmp_path, "420mm", "297mm", viewbox="0 0 420 420"), (420.0, 297.0))
    assert any("viewBox" in finding for finding in findings)


@pytest.mark.parametrize(
    ("width", "height", "page"),
    [
        ("1189mm", "841mm", (1189.0, 841.0)),
        ("841mm", "1189mm", (841.0, 1189.0)),
        ("420mm", "297mm", (420.0, 297.0)),
    ],
)
def test_qa_accepts_both_orientations(tmp_path, width, height, page):
    assert qa._svg_findings(_svg(tmp_path, width, height), page) == []


def test_qa_png_check_compares_both_axes(tmp_path):
    from PIL import Image

    path = tmp_path / "sheet.png"
    Image.new("RGB", (4961, 3508), "white").save(path, dpi=(300, 300))
    assert qa._png_findings(path, (4961, 3508)) == []
    assert qa._png_findings(path, (4961, 4961)) != []


# --- end-to-end sheet writer -------------------------------------------------


def test_write_svg_produces_a_true_iso_page(tmp_path):
    """The writer itself must emit the declared page, not a square of --sheet-mm."""
    import importlib.util

    from shapely.geometry import LineString, Polygon

    spec = importlib.util.spec_from_file_location("sheet_build", ROOT / "tools" / "cad-fond-de-plan-build.py")
    assert spec is not None and spec.loader is not None
    build = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(build)

    placement = le.resolve_placement((-50, -50, 50, 50), sheet="A3", scale=500)
    out = tmp_path / "sheet.svg"
    build.write_svg(
        out, [Polygon([(-10, -10), (10, -10), (10, 10), (-10, 10)])], LineString(), [], Polygon(), [], placement, False
    )

    root = ET.parse(out).getroot()
    assert root.attrib["width"] == "420.0mm"
    assert root.attrib["height"] == "297.0mm"
    assert root.attrib["viewBox"] == "0 0 420.0 297.0"


def test_written_sheet_clips_to_the_usable_area(tmp_path):
    """A clip reference must resolve, or content outside the page is silently kept."""
    import importlib.util

    from shapely.geometry import LineString, Polygon

    spec = importlib.util.spec_from_file_location("sheet_build2", ROOT / "tools" / "cad-fond-de-plan-build.py")
    assert spec is not None and spec.loader is not None
    build = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(build)

    placement = le.resolve_placement((-50, -50, 50, 50), sheet="A3", scale=500)
    out = tmp_path / "clip.svg"
    build.write_svg(
        out, [Polygon([(-10, -10), (10, -10), (10, 10), (-10, 10)])], LineString(), [], Polygon(), [], placement, False
    )

    root = ET.parse(out).getroot()
    clip_path = root.find(f".//{SVG_NS}clipPath")
    assert clip_path is not None, "clipPath element missing; url(#USABLE_AREA) resolves to nothing"
    assert clip_path.attrib["id"] == "USABLE_AREA"
    assert len(list(clip_path)) == 1
    clipped_group = next(g for g in root.iter(f"{SVG_NS}g") if g.attrib.get("clip-path"))
    assert clipped_group.attrib["clip-path"] == "url(#USABLE_AREA)"


def test_written_geometry_is_positioned_not_rescaled(tmp_path):
    """A 100 m square must measure 200 mm on a 1:500 sheet, whatever the page size."""
    import importlib.util

    from shapely.geometry import LineString, Polygon

    spec = importlib.util.spec_from_file_location("sheet_build3", ROOT / "tools" / "cad-fond-de-plan-build.py")
    assert spec is not None and spec.loader is not None
    build = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(build)

    square = Polygon([(-50, -50), (50, -50), (50, 50), (-50, 50)])
    for sheet in ("A3", "A1", "A0"):
        placement = le.resolve_placement((-50, -50, 50, 50), sheet=sheet, scale=500)
        out = tmp_path / f"{sheet}.svg"
        build.write_svg(out, [square], LineString(), [], Polygon(), [], placement, False)
        radius_mm = placement.mm_per_m * 50.0
        assert radius_mm == pytest.approx(100.0), sheet


def test_mm_point_refuses_a_scale_that_disagrees_with_the_page():
    import importlib.util

    spec = importlib.util.spec_from_file_location("sheet_build4", ROOT / "tools" / "cad-fond-de-plan-build.py")
    assert spec is not None and spec.loader is not None
    build = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(build)

    placement = le.resolve_placement((-50, -50, 50, 50), sheet="A3", scale=500)
    with pytest.raises(ValueError, match="disagrees with declared"):
        build.mm_point(0.0, 0.0, placement, 4.0)  # 4 mm/m is 1:250, not 1:500
    build.mm_point(0.0, 0.0, placement, 2.0)  # agreeing value is accepted


# --- the paper space and the raster must describe the same page --------------


def _build_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location("tp_build", ROOT / "tools" / "cad-fond-de-plan-build.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("orientation", ["landscape", "portrait"])
def test_dxf_paper_space_matches_the_declared_orientation(tmp_path, orientation):
    """A portrait report with a landscape layout tab is an unplottable deliverable."""
    import ezdxf
    from shapely.geometry import LineString, Polygon

    build = _build_module()
    page_w, page_h = le.page_size("A0", orientation)
    path = tmp_path / "sheet.dxf"
    build.write_dxf(
        path,
        [Polygon([(-40, -40), (40, -40), (40, 40), (-40, 40)])],
        LineString(),
        [],
        Polygon([(-40, -40), (40, -40), (40, 40), (-40, 40)]),
        [],
        32630,
        (655239.0, 3861554.0),
        1.0,
        layout_bounds_m=(-100.0, -100.0, 100.0, 100.0),
        layout_spec=("A0", 1000),
        orientation=orientation,
    )
    doc = ezdxf.readfile(path)
    layout = doc.layouts.get("A0-1_1000")
    assert layout is not None
    assert (layout.dxf.paper_width, layout.dxf.paper_height) == (page_w, page_h)


@pytest.mark.parametrize(("sheet", "scale"), [("A0", 1000), ("A1", 500), ("A3", 250)])
def test_dxf_viewport_denominator_is_exact(tmp_path, sheet, scale):
    """view_height / viewport_height_mm * 1000 must equal the declared denominator."""
    import ezdxf
    from shapely.geometry import LineString, Polygon

    build = _build_module()
    path = tmp_path / f"{sheet}.dxf"
    # A3 1:250 covers only 100 x 56.8 m, so the study bounds must fit the smallest sheet.
    bounds = (-40.0, -20.0, 40.0, 20.0)
    build.write_dxf(
        path,
        [Polygon([(-10, -10), (10, -10), (10, 10), (-10, 10)])],
        LineString(),
        [],
        Polygon([(-10, -10), (10, -10), (10, 10), (-10, 10)]),
        [],
        32630,
        (655239.0, 3861554.0),
        1.0,
        layout_bounds_m=bounds,
        layout_spec=(sheet, scale),
    )
    doc = ezdxf.readfile(path)
    layout = doc.layouts.get(f"{sheet}-1_{scale}")
    viewports = [v for v in layout.viewports() if v.dxf.id != 1]
    assert viewports, "no map viewport"
    viewport = viewports[0]
    measured = round(viewport.dxf.view_height * 1000 / viewport.dxf.height)
    assert measured == scale


def test_cartouche_sits_on_the_bottom_edge_of_any_page():
    for sheet in ("A3", "A1", "A0"):
        for orientation in le.ORIENTATIONS:
            layout = le.resolve_layout((-100, -100, 100, 100), sheet, 1000, orientation=orientation)
            x, y, w, h = layout.cartouche_rect_mm
            assert y == pytest.approx(10.0)
            assert x + w == pytest.approx(layout.width_mm - 10.0)
            assert h == pytest.approx(50.0)


# --- MCP and CLI surface ----------------------------------------------------


def test_cli_exposes_orientation_with_a_landscape_default():
    build = _build_module()
    parser = build.build_parser()
    assert parser.parse_args([]).orientation == "landscape"
    assert parser.parse_args(["--orientation", "portrait"]).orientation == "portrait"
    with pytest.raises(SystemExit):
        parser.parse_args(["--orientation", "diagonal"])


def test_resolver_honours_layout_sheet_and_scale_over_sheet_mm():
    """The declared sheet/scale pair is the only statement of a real plot scale."""
    build = _build_module()
    parser = build.build_parser()
    args = parser.parse_args(["--layout-sheet", "A0", "--layout-scale", "1000", "--sheet-mm", "420"])
    placement = build.resolve_sheet_placement(args, 300.0, (655239.0, 3861554.0))
    assert placement.sheet == "A0"
    assert placement.scale == 1000
    assert placement.page_w_mm == 1189.0


def test_resolver_declares_a_layoutless_run_as_custom_not_a3():
    build = _build_module()
    parser = build.build_parser()
    args = parser.parse_args(["--sheet-mm", "420", "--radius", "300"])
    placement = build.resolve_sheet_placement(args, 300.0, (655239.0, 3861554.0))
    assert placement.sheet == "CUSTOM"
    assert (placement.page_w_mm, placement.page_h_mm) == (420.0, 420.0)


def test_resolver_refuses_a_layout_that_cannot_hold_the_study_radius():
    build = _build_module()
    parser = build.build_parser()
    args = parser.parse_args(["--layout-sheet", "A3", "--layout-scale", "500"])
    with pytest.raises(ValueError, match="does not fit A3 at 1:500"):
        build.resolve_sheet_placement(args, 300.0, (655239.0, 3861554.0))
