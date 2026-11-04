"""Fast geometry-only regression tests for the precision audit; no render/export calls."""
import importlib.util
import math
import unittest
from pathlib import Path

import geopandas as gpd
from pyproj import Transformer
from shapely.affinity import rotate
from shapely.geometry import LineString, Polygon, box

TOOL = Path(__file__).resolve().parents[1] / "tools" / "cad-fond-de-plan-build.py"
spec = importlib.util.spec_from_file_location("fond_build_precision", TOOL)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


class PrecisionEdgeCases(unittest.TestCase):
    def test_oblique_vernacular_l_shape_keeps_its_local_axis_and_concavity(self):
        theta = math.radians(23.0)
        source = rotate(Polygon([(0, 0), (12, 0), (12, 4), (4, 4), (4, 10), (0, 10)]),
                        23.0, origin=(0, 0))
        result, fallback = mod.orthogonalize(source, theta, min_segment=1.2)
        self.assertFalse(fallback)
        self.assertAlmostEqual(result.area, source.area, places=6)
        self.assertEqual(len(result.exterior.coords), len(source.exterior.coords))
        for a, b in zip(result.exterior.coords, list(result.exterior.coords)[1:]):
            if math.dist(a, b) > 1e-8:
                edge_angle = math.atan2(b[1] - a[1], b[0] - a[0])
                self.assertLess(abs(math.sin(2.0 * (edge_angle - theta))), 1e-7)
        noisy = Polygon([(0, 0), (16, 0), (15, 4), (8.2, 4.2), (8, 10), (0, 10)])
        corrected, _ = mod.orthogonalize(noisy, 0.0, min_segment=1.2)
        self.assertLessEqual(abs(corrected.area - noisy.area) / noisy.area, 0.05)
        _, noisy_aspect = mod.rect_axis(noisy)
        _, corrected_aspect = mod.rect_axis(corrected)
        self.assertLessEqual(abs(corrected_aspect - noisy_aspect) / noisy_aspect, 0.05)

    def test_multiple_rotated_courtyards_survive(self):
        source = Polygon(
            [(0, 0), (20, 0), (20, 15), (0, 15)],
            holes=[[(3, 3), (6, 3), (6, 7), (3, 7)], [(12, 5), (17, 5), (17, 10), (12, 10)]],
        )
        source = rotate(source, 17.0, origin=(0, 0))
        result, _ = mod.orthogonalize(source, math.radians(17.0), min_segment=1.2)
        self.assertEqual(len(result.interiors), 2)
        self.assertAlmostEqual(result.area, 263.0, places=5)
        self.assertTrue(result.is_valid)

    def test_angled_shared_party_wall_remains_coincident_with_common_axis(self):
        theta = math.radians(23.0)
        left = rotate(Polygon([(0, 0), (10, 0), (10, 8), (0, 8)]), 23.0, origin=(0, 0))
        right = rotate(Polygon([(10, 0), (20, 0), (20, 8), (10, 8)]), 23.0, origin=(0, 0))
        a, _ = mod.orthogonalize(left, theta)
        b, _ = mod.orthogonalize(right, theta)
        self.assertAlmostEqual(a.boundary.intersection(b.boundary).length, 8.0, places=6)
        self.assertAlmostEqual(a.intersection(b).area, 0.0, places=8)

    def test_minimum_rectangle_aspect_ratio_uses_short_side(self):
        """MRR ratio, constrained correction, and separate local axis modes."""
        long_rectangle = Polygon([(0, 0), (16, 0), (16, 3), (0, 3)])
        _, aspect = mod.rect_axis(long_rectangle)
        self.assertAlmostEqual(aspect, 16.0 / 3.0, places=6)
        theta = math.radians(27.0)
        rotated = rotate(long_rectangle, 27.0, origin=(0, 0))
        snapped, fallback = mod.orthogonalize(rotated, theta)
        self.assertFalse(fallback)
        self.assertLessEqual(abs(snapped.area - rotated.area) / rotated.area, 0.05)
        _, snapped_aspect = mod.rect_axis(snapped)
        self.assertLessEqual(abs(snapped_aspect - aspect) / aspect, 0.05)

        horizontal = Polygon([(0, 0), (16, 0), (16, 3), (0, 3)])
        oblique = rotate(Polygon([(8, 5), (24, 5), (24, 8), (8, 8)]),
                         30.0, origin=(8, 5))
        axes = mod.building_axes([horizontal, oblique], radius_m=35.0)
        self.assertLess(mod.axial_difference(axes[0], 0.0), math.radians(1.0))
        self.assertLess(mod.axial_difference(axes[1], math.radians(30.0)), math.radians(1.0))

    def _manual_casement(self, roads):
        empty_osm = gpd.GeoDataFrame(geometry=[], crs="EPSG:32630")
        identity = Transformer.from_crs(32630, 32630, always_xy=True)
        return mod.road_casements(
            empty_osm, identity, box(-50, -50, 50, 50), 0.0, 0.0, roads
        )

    def test_acute_t_junction_casing_is_valid_and_single_component(self):
        casing, osm_count, manual_count = self._manual_casement([
            (LineString([(-20, 0), (20, 0)]), 8.0),
            (LineString([(0, 0), (20, 4.2)]), 5.0),  # 12-degree branch into the trunk
        ])
        self.assertEqual(osm_count, 0)
        self.assertEqual(manual_count, 2)
        self.assertTrue(casing.is_valid)
        self.assertEqual(len(mod.flatten_polygons(casing)), 1)

    def test_dead_end_uses_flat_terminus_at_centerline_endpoint(self):
        casing, _, _ = self._manual_casement([
            (LineString([(0, 0), (10, 0)]), 4.0),
        ])
        self.assertTrue(casing.is_valid)
        self.assertEqual(tuple(round(v, 6) for v in casing.bounds), (0.0, -2.0, 10.0, 2.0))

    def test_current_north_up_pixel_mapping_uses_positive_east_negative_row(self):
        anchor = (493.0, 680.0)
        e0, n0 = 655840.064214, 3860557.983806
        east, north = mod.pixel_to_world((503.0, 660.0), e0, n0, anchor, 0.12)
        self.assertAlmostEqual(east, e0 + 1.2, places=8)
        self.assertAlmostEqual(north, n0 + 2.4, places=8)
        # An anchor-only check cannot estimate/correct the GSD; it merely fixes translation.
        self.assertEqual(mod.pixel_to_world(anchor, e0, n0, anchor, 0.12), (e0, n0))


if __name__ == "__main__":
    unittest.main()
