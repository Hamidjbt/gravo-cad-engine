"""Regression tests for deterministic urban topology and metric paper layouts.

These operate on synthetic geometry and temporary DXF files only; they never invoke
network acquisition, plan generation, raster rendering, or exports to site-plan.
"""

from __future__ import annotations

import importlib.util
import math
import sys
from pathlib import Path

import ezdxf
import pytest
from shapely.geometry import LineString, Polygon

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
sys.path.insert(0, str(TOOLS))

from layout_engine import (  # noqa: E402
    add_cartouche_svg,
    metadata_from_utm,
    resolve_layout,
    resolve_sheet_set,
)
from urban_topology import (  # noqa: E402
    build_road_graph,
    courtyard_widths,
    reconcile_parcel_frontages,
    road_corridor_from_graph,
    snap_shared_walls,
)

spec = importlib.util.spec_from_file_location("fond_build", TOOLS / "cad-fond-de-plan-build.py")
assert spec is not None and spec.loader is not None
build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build)


def test_shared_wall_midline_snap_is_valid_and_area_bounded():
    left = Polygon([(0, 0), (10, 0), (10, 10), (0, 10)])
    right = Polygon([(10.2, 0.1), (20.2, 0.1), (20.2, 10.1), (10.2, 10.1)])
    result = snap_shared_walls([left, right], max_gap_m=0.8)
    assert all(poly.is_valid for poly in result.footprints)
    assert all(
        abs(new.area - old.area) / old.area <= 0.05 for old, new in zip((left, right), result.footprints, strict=True)
    )
    assert result.adjacency.has_edge(0, 1)
    assert sum(line.length for line in result.shared_walls) >= 9.0


def test_courtyard_ring_is_retained_and_width_diagnosed():
    source = Polygon([(0, 0), (20, 0), (20, 20), (0, 20)], holes=[[(5, 5), (5, 8), (11, 8), (11, 5)]])
    result = snap_shared_walls([source])
    assert len(result.footprints[0].interiors) == 1
    assert courtyard_widths(result.footprints[0]) == pytest.approx((3.0,))
    assert result.courtyard_widths_m == {0: (3.0,)}
    assert not any("patio retained" in msg for msg in result.diagnostics)


def test_sub_two_metre_courtyard_is_kept_and_flagged_not_pruned():
    source = Polygon([(0, 0), (20, 0), (20, 20), (0, 20)], holes=[[(5, 5), (5, 6.5), (11, 6.5), (11, 5)]])
    result = snap_shared_walls([source])
    assert len(result.footprints[0].interiors) == 1
    assert any("sub-2m patio retained" in msg for msg in result.diagnostics)


def test_road_graph_nodes_same_level_crossing_and_builds_valid_corridor():
    segments = [
        (LineString([(-10, 0), (10, 0)]), 4.0, {"source": "synthetic"}),
        (LineString([(0, -10), (0, 10)]), 6.0, {"source": "synthetic"}),
    ]
    graph = build_road_graph(segments)
    crossing = [
        node for node, data in graph.nodes(data=True) if math.isclose(data["x"], 0) and math.isclose(data["y"], 0)
    ]
    assert len(crossing) == 1
    assert graph.degree(crossing[0]) == 4
    corridor = road_corridor_from_graph(graph)
    assert corridor.is_valid
    assert corridor.area > 0


def test_grade_separated_road_crossing_remains_unconnected():
    segments = [
        (LineString([(-10, 0), (10, 0)]), 4.0, {"layer": "0"}),
        (LineString([(0, -10), (0, 10)]), 4.0, {"layer": "1", "bridge": "yes"}),
    ]
    graph = build_road_graph(segments)
    crossing_nodes = [node for node, data in graph.nodes(data=True) if data["x"] == 0 and data["y"] == 0]
    assert len(crossing_nodes) == 0
    assert graph.number_of_edges() == 2


def test_near_curb_parcel_frontage_reconciles_without_moving_distant_vertices():
    graph = build_road_graph([(LineString([(0, 0), (20, 0)]), 4.0, {})])
    road = road_corridor_from_graph(graph)
    frontage = LineString([(2, 2.15), (8, 2.15), (8, 8)])
    output, changed = reconcile_parcel_frontages([frontage], road, tolerance_m=0.2)
    assert changed == 1
    assert output[0].coords[0][1] == pytest.approx(2.0, abs=0.01)
    assert output[0].coords[-1] == pytest.approx((8.0, 8.0))


def test_standard_sheet_set_uses_stated_scales_and_fixed_titleblock():
    layouts = resolve_sheet_set((0, 0, 40, 25))
    assert [(item.sheet, item.scale) for item in layouts] == [("A3", 250), ("A1", 500), ("A0", 1000)]
    assert all(item.cartouche_rect_mm[2:] == (180.0, 50.0) for item in layouts)
    assert all(
        item.viewport_rect_mm[2] * item.scale / 1000.0 == pytest.approx(item.ground_extent_m[0]) for item in layouts
    )
    with pytest.raises(ValueError, match="does not fit"):
        resolve_layout((0, 0, 200, 100), "A3", 250)


def test_utm_metadata_and_svg_cartouche_use_millimetre_type():
    metadata = metadata_from_utm(500000, 3850000, 32630, 1234.5)
    assert metadata["utm"]["epsg"] == "EPSG:32630"
    assert -4.0 < metadata["wgs84"]["longitude"] < -2.0
    import svgwrite

    layout = resolve_layout((0, 0, 40, 25), "A3", 250)
    drawing = svgwrite.Drawing(size=("420mm", "297mm"), viewBox="0 0 420 297")
    add_cartouche_svg(drawing, layout, metadata)
    markup = drawing.tostring()
    assert "180.0" in markup and "2.4mm" in markup and "50 m" in markup


def test_r2018_dxf_has_architectural_layers_blocks_and_three_paper_layouts(tmp_path):
    site = Polygon([(0, 0), (40, 0), (40, 25), (0, 25)])
    building = Polygon([(5, 5), (15, 5), (15, 12), (5, 12)], holes=[[(8, 7), (8, 9), (10, 9), (10, 7)]])
    path = tmp_path / "synthetic-urban.dxf"
    build.write_dxf(
        path,
        [building],
        LineString([(0, 0), (20, 0)]),
        [LineString([(0, 2), (10, 2)])],
        site,
        [],
        32630,
        (20, 12.5),
        0.001,
        road_centerlines=[LineString([(0, 0), (20, 0)])],
        shared_walls=[],
        layout_bounds_m=site.bounds,
    )
    doc = ezdxf.readfile(path)
    names = {layer.dxf.name for layer in doc.layers}
    required = {
        "G-BLDG-WALL",
        "G-BLDG-SHARED",
        "G-ROAD-CURB",
        "G-ROAD-CL",
        "G-PARCEL",
        "G-HATCH",
        "G-SITE-BOUNDARY",
        "G-ANNO-GRID",
        "G-ANNO-NORTH",
        "G-ANNO-TTLB",
    }
    assert required <= names
    assert all(doc.layers.get(name).dxf.color == 7 for name in required)
    assert doc.layers.get("G-ROAD-CL").dxf.linetype == "CENTER"
    assert doc.layers.get("G-SITE-BOUNDARY").dxf.linetype == "SITE_DASH"
    assert doc.dxfversion == "AC1032"
    assert doc.header["$INSUNITS"] == 6
    assert "GRID_CROSS" in doc.blocks and "NORTH_ARROW" in doc.blocks
    assert {layout.name for layout in doc.layouts} >= {"A3-1_250", "A1-1_500", "A0-1_1000"}
    assert len(list(doc.layouts.get("A3-1_250").query("VIEWPORT"))) >= 1
    assert len(list(doc.layouts.get("A3-1_250").query("TEXT"))) >= 5
    assert len(list(doc.layouts.get("A3-1_250").query("INSERT"))) >= 2  # grid-cross + north arrow blocks
    assert len(list(doc.modelspace().query("INSERT"))) == 0
    # Inner courtyard boundary must be emitted alongside the exterior ring.
    building_entities = [entity for entity in doc.modelspace().query("LWPOLYLINE") if entity.dxf.layer == "G-BLDG-WALL"]
    assert len(building_entities) == 2
