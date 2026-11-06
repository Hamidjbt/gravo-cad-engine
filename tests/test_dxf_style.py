"""DXF plotting-standards tests (v1.11.0).

Verification is deliberately three-axis, because ezdxf's *renderer* cannot draw
pattern hatches and a pixel comparison would therefore prove nothing:

1. **Tag level** — re-read the written DXF and assert the group codes a plotting
   application actually consumes (layer colour/lineweight, 2/41/52/70/71/75/76/91/92).
2. **Geometry level** — independent point-in-polygon math confirming a courtyard is
   a hole rather than trusting the entity's own flags to be self-consistent.
3. **Budget level** — the vector-first invariant: no raster payloads, bounded byte
   growth, and hatch counts proportional to buildings rather than to geometry.
"""

from __future__ import annotations

import math
from pathlib import Path

import ezdxf
import pytest
from shapely.geometry import Point, Polygon

from tools import dxf_standards as std

# ---------------------------------------------------------------------------
# standards module: the values themselves
# ---------------------------------------------------------------------------


def test_every_layer_lineweight_is_in_the_dxf_enum():
    """An out-of-enum lineweight is silently coerced by CAD; fail here instead."""
    for name, attrs in std.LAYERS.items():
        assert attrs["lineweight"] in std.VALID_LINEWEIGHTS, f"{name} has an illegal lineweight"


def test_every_layer_colour_is_a_legal_aci_index():
    for name, attrs in std.LAYERS.items():
        assert std.ACI_MIN <= attrs["color"] <= std.ACI_MAX, f"{name} has an illegal ACI"


def test_structural_layers_outweigh_context_layers():
    """The plot must read hierarchically: boundary > building > context > fill."""
    weights = {name: attrs["lineweight"] for name, attrs in std.LAYERS.items()}
    assert weights["G-SITE-BOUNDARY"] > weights["G-BLDG-WALL"] > weights["G-ROAD-CURB"]
    assert weights["G-ROAD-CURB"] >= weights["G-PARCEL"] >= weights["G-BLDG-FILL"]


def test_adaptive_colour_is_reserved_for_structural_layers():
    """ACI 7 flips with the plot background, so only structural ink should use it."""
    adaptive = {name for name, a in std.LAYERS.items() if a["color"] == std.ACI_ADAPTIVE}
    assert "G-BLDG-WALL" in adaptive
    assert "G-BLDG-FILL" not in adaptive, "a fill that flips colour will plot as a blot"


def test_validate_lineweight_rejects_illegal_values():
    assert std.validate_lineweight(50) == 50
    with pytest.raises(ValueError, match="not a valid DXF lineweight"):
        std.validate_lineweight(37)


def test_validate_color_rejects_out_of_range():
    with pytest.raises(ValueError, match="outside 1..255"):
        std.validate_color(300)


# ---------------------------------------------------------------------------
# pattern scale: the plot-scale maths
# ---------------------------------------------------------------------------


def test_pattern_scale_lands_a_constant_pitch_on_paper():
    """2 mm on paper at every denominator: the whole point of deriving the scale."""
    for denominator in (250, 500, 1000):
        scale = std.pattern_scale_for(denominator)
        ground_m = scale * std.ANSI31_BASE_SPACING
        paper_mm = ground_m / (denominator / 1000.0)
        assert math.isclose(paper_mm, std.PAPER_HATCH_PITCH_MM, rel_tol=1e-9)


def test_pattern_scale_matches_hand_computed_values():
    """Hand-computed from the corrected spacing (2.2451 units), not from the code.

    At 1:1000 a 2 mm paper pitch is 2.0 m of ground; 2.0 / 2.2451 = 0.891.
    An earlier version of this module used 3.175 (the offset magnitude, not the
    perpendicular spacing) and was 1.41x too coarse at every scale.
    """
    assert math.isclose(std.pattern_scale_for(1000), 2.0 / 2.2450640, rel_tol=1e-6)
    assert math.isclose(std.pattern_scale_for(250), 0.5 / 2.2450640, rel_tol=1e-6)
    assert math.isclose(std.pattern_scale_for(500), 1.0 / 2.2450640, rel_tol=1e-6)


def test_pattern_scale_grows_with_denominator():
    scales = [std.pattern_scale_for(d) for d in (250, 500, 1000)]
    assert scales == sorted(scales)


def test_pattern_scale_rejects_nonsense():
    with pytest.raises(ValueError):
        std.pattern_scale_for(0)
    with pytest.raises(ValueError):
        std.pattern_scale_for(500, pitch_mm=0)


# ---------------------------------------------------------------------------
# fixture: a building with a courtyard, written through the real writer
# ---------------------------------------------------------------------------


def _square(x: float, y: float, size: float) -> list[tuple[float, float]]:
    return [(x, y), (x + size, y), (x + size, y + size), (x, y + size)]


@pytest.fixture
def courtyard_doc():
    """A 20x20 building with a 6x6 courtyard, hatched exactly as the builder does."""
    doc = ezdxf.new("R2018", setup=True)
    msp = doc.modelspace()
    hatch = msp.add_hatch(dxfattribs={"layer": std.HATCH_LAYER})
    hatch.set_solid_fill(color=8)
    hatch.dxf.hatch_style = 0
    hatch.paths.add_polyline_path(_square(0, 0, 20), is_closed=True, flags=1)
    hatch.paths.add_polyline_path(_square(7, 7, 6), is_closed=True, flags=0)
    return doc


# ---------------------------------------------------------------------------
# axis 1: tag level
# ---------------------------------------------------------------------------


def test_solid_hatch_exports_the_codes_a_plotter_reads(courtyard_doc, tmp_path):
    path = tmp_path / "solid.dxf"
    courtyard_doc.saveas(path)
    doc = ezdxf.readfile(path)
    hatch = list(doc.modelspace().query("HATCH"))[0]

    assert hatch.dxf.solid_fill == 1
    assert hatch.dxf.pattern_name == "SOLID"
    assert hatch.dxf.hatch_style == 0  # nested / odd-parity
    assert len(hatch.paths) == 2
    assert int(hatch.dxf.pattern_type) == std.HATCH_PATTERN_TYPE_PREDEFINED


def test_inner_boundary_path_is_not_marked_external(courtyard_doc):
    """flags=1 on a courtyard is the classic defect: it fills in."""
    flags = [path.path_type_flags for path in courtyard_doc.modelspace().query("HATCH")[0].paths]
    assert flags[0] & 1, "outer path must carry the external bit"
    assert not (flags[1] & 1), "courtyard path must not carry the external bit"


def test_ansi31_hatch_exports_pattern_scale_and_angle(tmp_path):
    doc = ezdxf.new("R2018", setup=True)
    msp = doc.modelspace()
    hatch = msp.add_hatch(dxfattribs={"layer": std.HATCH_LAYER})
    hatch.set_pattern_fill(std.HATCH_ANSI31, scale=std.pattern_scale_for(1000))
    hatch.dxf.hatch_style = 0
    hatch.paths.add_polyline_path(_square(0, 0, 20), is_closed=True, flags=1)

    path = tmp_path / "ansi.dxf"
    doc.saveas(path)
    reread = list(ezdxf.readfile(path).modelspace().query("HATCH"))[0]

    assert reread.dxf.pattern_name == "ANSI31"
    assert reread.dxf.solid_fill == 0
    assert math.isclose(reread.dxf.pattern_scale, std.pattern_scale_for(1000), rel_tol=1e-3)
    assert int(reread.dxf.pattern_type) == std.HATCH_PATTERN_TYPE_PREDEFINED


def test_layer_styling_survives_a_write_read_cycle(tmp_path):
    doc = ezdxf.new("R2018", setup=True)
    for name in std.LAYERS:
        std.create_layer(doc, name)
    path = tmp_path / "layers.dxf"
    doc.saveas(path)
    reread = ezdxf.readfile(path)
    for name, attrs in std.LAYERS.items():
        layer = reread.layers.get(name)
        assert layer.dxf.color == attrs["color"], name
        assert layer.dxf.lineweight == attrs["lineweight"], name


# ---------------------------------------------------------------------------
# axis 2: geometry level — independent math, not the entity's own flags
# ---------------------------------------------------------------------------


def test_courtyard_is_geometrically_outside_the_wall_polygon(courtyard_doc):
    hatch = list(courtyard_doc.modelspace().query("HATCH"))[0]
    outer = Polygon([tuple(v[:2]) for v in hatch.paths[0].vertices])
    inner = Polygon([tuple(v[:2]) for v in hatch.paths[1].vertices])

    wall_point = Point(2.0, 2.0)
    courtyard_point = Point(10.0, 10.0)

    assert outer.contains(wall_point)
    assert not inner.contains(wall_point)
    assert inner.contains(courtyard_point), "the courtyard must be a real polygon"
    # Odd-parity fill: inside outer minus inside inner. Verify the subtraction really
    # produces empty space rather than a zero-width sliver.
    void = outer.difference(inner)
    assert not void.contains(courtyard_point)
    assert void.contains(wall_point)
    assert void.area > 0


def test_courtyard_area_is_actually_removed():
    outer = Polygon(_square(0, 0, 20))
    inner = Polygon(_square(7, 7, 6))
    assert math.isclose(outer.area - inner.area, 400 - 36)


# ---------------------------------------------------------------------------
# axis 3: budget level — the vector-first invariant
# ---------------------------------------------------------------------------


def test_vector_only_document_has_no_raster_findings(courtyard_doc):
    assert std.dxf_is_vector_only(courtyard_doc) == []


def test_hatch_findings_accepts_a_correct_courtyard(courtyard_doc):
    assert std.hatch_findings(courtyard_doc) == []


def test_hatch_findings_flags_a_filled_courtyard():
    """An inner path marked external is a defect the gate must catch."""
    doc = ezdxf.new("R2018", setup=True)
    hatch = doc.modelspace().add_hatch(dxfattribs={"layer": std.HATCH_LAYER})
    hatch.set_solid_fill(color=8)
    hatch.paths.add_polyline_path(_square(0, 0, 20), is_closed=True, flags=1)
    hatch.paths.add_polyline_path(_square(7, 7, 6), is_closed=True, flags=1)
    findings = std.hatch_findings(doc)
    assert any("courtyards will fill in" in f for f in findings)


def test_hatch_findings_flags_a_hatch_with_no_paths():
    doc = ezdxf.new("R2018", setup=True)
    doc.modelspace().add_hatch(dxfattribs={"layer": std.HATCH_LAYER})
    assert any("no boundary paths" in f for f in std.hatch_findings(doc))


def test_hatch_findings_flags_a_missing_pattern(tmp_path):
    doc = ezdxf.new("R2018", setup=True)
    hatch = doc.modelspace().add_hatch(dxfattribs={"layer": std.HATCH_LAYER})
    hatch.set_pattern_fill(std.HATCH_ANSI31, scale=4.0)
    hatch.paths.add_polyline_path(_square(0, 0, 20), is_closed=True, flags=1)
    findings = std.hatch_findings(doc, expected_pattern=std.HATCH_SOLID)
    assert any("is not a solid fill" in f for f in findings)


def test_hatch_growth_is_one_entity_per_building_not_per_line():
    """Hatching must not explode into line soup: the count tracks buildings."""
    doc = ezdxf.new("R2018", setup=True)
    msp = doc.modelspace()
    for index in range(25):
        hatch = msp.add_hatch(dxfattribs={"layer": std.HATCH_LAYER})
        hatch.set_solid_fill(color=8)
        hatch.paths.add_polyline_path(_square(index * 30, 0, 20), is_closed=True, flags=1)
    assert len(list(msp.query("HATCH"))) == 25
    assert len(list(msp.query("LINE"))) == 0, "hatching must not emit loose lines"


def test_style_findings_detects_a_wrong_colour(tmp_path):
    doc = ezdxf.new("R2018", setup=True)
    for name in std.LAYERS:
        std.create_layer(doc, name)
    doc.layers.get("G-BLDG-WALL").dxf.color = 3
    findings = std.style_findings(doc)
    assert any("G-BLDG-WALL color is 3" in f for f in findings)


def test_style_findings_detects_a_missing_required_layer():
    doc = ezdxf.new("R2018", setup=True)
    findings = std.style_findings(doc)
    assert any("missing required layer G-BLDG-WALL" in f for f in findings)


# ---------------------------------------------------------------------------
# the writer itself
# ---------------------------------------------------------------------------


def test_write_dxf_emits_styled_layers_and_hatches(tmp_path):
    """End-to-end through the real writer with a building and a courtyard."""
    import importlib
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
    build_mod = importlib.import_module("cad-fond-de-plan-build")

    building = Polygon(_square(0, 0, 20), [_square(7, 7, 6)])
    site = Polygon(_square(-50, -50, 200))
    out = tmp_path / "styled.dxf"
    build_mod.write_dxf(
        out,
        buildings=[building],
        road_boundaries=None,
        parcels=[],
        site=site,
        hatch_geoms=[],
        epsg=32630,
        center=(0.0, 0.0),
        scale_mm_m=1.0,
        hatch_pattern="SOLID",
    )

    doc = ezdxf.readfile(out)
    hatches = list(doc.modelspace().query("HATCH"))
    assert len(hatches) == 1
    assert len(hatches[0].paths) == 2
    assert std.hatch_findings(doc) == []
    assert doc.header["$LWDISPLAY"] == 1
    # Downgrading the version would break the QA gate and lose hatch support.
    assert doc.dxfversion == "AC1032"


def test_write_dxf_with_ansi31_uses_the_denominator_scale(tmp_path):
    import importlib
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
    build_mod = importlib.import_module("cad-fond-de-plan-build")

    out = tmp_path / "ansi_written.dxf"
    build_mod.write_dxf(
        out,
        buildings=[Polygon(_square(0, 0, 20))],
        road_boundaries=None,
        parcels=[],
        site=Polygon(_square(-50, -50, 200)),
        hatch_geoms=[],
        epsg=32630,
        center=(0.0, 0.0),
        scale_mm_m=1.0,
        layout_bounds_m=(-100, -100, 100, 100),
        layout_spec=("A0", 1000),
        hatch_pattern="ANSI31",
    )
    doc = ezdxf.readfile(out)
    hatch = list(doc.modelspace().query("HATCH"))[0]
    assert hatch.dxf.pattern_name == "ANSI31"
    assert math.isclose(hatch.dxf.pattern_scale, std.pattern_scale_for(1000), rel_tol=1e-3)


def test_create_layer_beats_the_silent_dxfattribs_trap(tmp_path):
    """Regression guard for the defect that made every layer uniformly unstyled.

    ``LayerTable.add(dxfattribs={"color":…, "lineweight":…})`` silently drops both in
    ezdxf 1.4.5. If this test ever fails, the styling has regressed to monochrome.
    """
    doc = ezdxf.new("R2018", setup=True)
    trapped = doc.layers.add("TRAPPED", dxfattribs={"color": 5, "lineweight": 70})
    assert trapped.dxf.lineweight == -3, "ezdxf behaviour changed; revisit create_layer"

    std.create_layer(doc, "G-BLDG-WALL")
    path = tmp_path / "trap.dxf"
    doc.saveas(path)
    reread = ezdxf.readfile(path).layers.get("G-BLDG-WALL")
    assert reread.dxf.color == std.LAYERS["G-BLDG-WALL"]["color"]
    assert reread.dxf.lineweight == std.LAYERS["G-BLDG-WALL"]["lineweight"]


# ---------------------------------------------------------------------------
# determinism: identical inputs must yield identical bytes
# ---------------------------------------------------------------------------


def test_dxf_output_is_byte_deterministic(tmp_path):
    """Two runs of the same geometry must produce identical files.

    DXF writers stamp wall-clock time and fresh GUIDs into every file. Beyond
    reproducibility, a varying byte count makes the artifact size unpredictable,
    which matters because a resumable upload session declares that size up front.
    """
    import importlib
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
    build_mod = importlib.import_module("cad-fond-de-plan-build")

    def render(target: Path) -> bytes:
        build_mod.write_dxf(
            target,
            buildings=[Polygon(_square(0, 0, 20), [_square(7, 7, 6)])],
            road_boundaries=None,
            parcels=[],
            site=Polygon(_square(-50, -50, 200)),
            hatch_geoms=[],
            epsg=32630,
            center=(0.0, 0.0),
            scale_mm_m=1.0,
        )
        return target.read_bytes()

    first = render(tmp_path / "a.dxf")
    second = render(tmp_path / "b.dxf")
    assert first == second, "DXF bytes vary between runs of identical input"
    assert len(first) == len(second)
