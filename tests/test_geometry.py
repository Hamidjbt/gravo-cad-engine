import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import importlib.util

TOOL = Path(__file__).resolve().parents[1] / "tools" / "cad-fond-de-plan-build.py"
spec = importlib.util.spec_from_file_location("fond_build", TOOL)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
from shapely.geometry import Polygon


class MetricAndOrthogonalization(unittest.TestCase):
    def test_tlemcen_center_uses_utm_zone_30_north(self):
        self.assertEqual(mod.utm_epsg(34.8754565, -1.2948622), 32630)

    def test_minimum_rectangle_and_cluster_axis_preserve_l_shape(self):
        l_shape = Polygon([(0, 0), (8, 0), (8, 3), (3, 3), (3, 8), (0, 8)])
        axis, aspect = mod.rect_axis(l_shape)
        out, fallback = mod.orthogonalize(l_shape, axis)
        self.assertFalse(fallback)
        self.assertAlmostEqual(out.area, l_shape.area, places=5)
        self.assertEqual(len(out.exterior.coords), 7)
        self.assertGreaterEqual(aspect, 1.0)

    def test_courtyard_hole_survives_orthogonalization(self):
        poly = Polygon([(0, 0), (12, 0), (12, 10), (0, 10)],
                       holes=[[(3, 3), (3, 7), (8, 7), (8, 3)]])
        out, _ = mod.orthogonalize(poly, 0.0)
        self.assertEqual(len(out.interiors), 1)
        self.assertAlmostEqual(out.area, 100.0, places=4)

    def test_micro_vertices_are_removed_before_snapping(self):
        ring = [(0, 0), (5, 0), (5.4, 0.1), (5, 4), (0, 4), (0, 0)]
        cleaned = mod.clean_ring(ring, 1.2)
        self.assertLess(len(cleaned), 5)

    def test_cluster_axes_are_axial_not_directional(self):
        p1 = Polygon([(0, 0), (8, 0), (8, 4), (0, 4)])
        p2 = Polygon([(12, 1), (20, 1), (20, 5), (12, 5)])
        axes = mod.building_axes([p1, p2])
        self.assertLess(abs(axes[0]), 1e-6)
        self.assertLess(abs(axes[1]), 1e-6)

    def test_hatching_is_clipped_to_polygon_and_hole(self):
        poly = Polygon([(0, 0), (10, 0), (10, 10), (0, 10)],
                       holes=[[(4, 4), (6, 4), (6, 6), (4, 6)]])
        lines = mod.line_hatches(poly, 1.0)
        self.assertTrue(lines)
        self.assertTrue(all(poly.buffer(1e-8).covers(line) for line in lines))
        self.assertTrue(all(line.intersection(Polygon([(4,4),(6,4),(6,6),(4,6)])).length < 1e-7 for line in lines))

    def test_photo_anchor_maps_exactly_to_center_coordinate(self):
        p = mod.pixel_to_world((493, 680), 655840.064214, 3860557.983806, (493, 680), 0.12)
        self.assertAlmostEqual(p[0], 655840.064214)
        self.assertAlmostEqual(p[1], 3860557.983806)

    def test_utm_quadkey_tile_is_stable(self):
        self.assertEqual(mod.quadkey(34.8754565, -1.2948622, 9), "033113132")


if __name__ == "__main__":
    unittest.main()
