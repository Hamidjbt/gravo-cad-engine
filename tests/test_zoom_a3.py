import importlib.util
import math
import unittest
from pathlib import Path
import xml.etree.ElementTree as ET

TOOL = Path(__file__).resolve().parents[1] / "tools" / "cad-fond-de-plan-zoom-a3.py"
spec = importlib.util.spec_from_file_location("zoom_a3", TOOL)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
from shapely.geometry import Polygon


class ReadOnlyA3Layout(unittest.TestCase):
    def test_a3_landscape_crop_is_exactly_one_to_two_fifty(self):
        x0, y0, w, h, zoom = mod.crop_window(215.628, 200.172)
        self.assertAlmostEqual(w, 73.5, places=7)
        self.assertAlmostEqual(h, 51.975, places=7)
        self.assertAlmostEqual(w / h, 420 / 297, places=7)
        self.assertAlmostEqual(zoom, 40 / 7, places=7)
        self.assertAlmostEqual((w / 0.7), 105.0, places=6)
        self.assertAlmostEqual((h / 0.7), 74.25, places=6)
        self.assertAlmostEqual(x0 + w / 2, 215.628)
        self.assertAlmostEqual(y0 + h / 2, 200.172)

    def test_path_parser_preserves_outer_ring_and_courtyard(self):
        d = "M 0,0 L 10,0 L 10,10 L 0,10 Z M 3,3 L 3,7 L 7,7 L 7,3 Z"
        rings = mod.parse_path_rings(d)
        self.assertEqual(len(rings), 2)
        poly = mod.polygon_from_path(d)
        self.assertAlmostEqual(poly.area, 84.0)
        self.assertEqual(len(poly.interiors), 1)

    def test_hatch_spacing_is_compensated_for_zoom(self):
        _, _, _, _, zoom = mod.crop_window(0, 0)
        pitch = mod.PAPER_HATCH_MM / zoom
        poly = Polygon([(0,0),(12,0),(12,9),(0,9)], holes=[[(4,3),(8,3),(8,6),(4,6)]])
        lines = mod.hatch_lines(poly, pitch)
        self.assertTrue(lines)
        self.assertTrue(all(poly.buffer(1e-8).covers(line) for line in lines))
        self.assertAlmostEqual(pitch * zoom, mod.PAPER_HATCH_MM)

    def test_paper_strokes_are_compensated_not_restyled(self):
        _, _, _, _, zoom = mod.crop_window(0, 0)
        g = ET.Element("g", {"stroke-width":"0.50", "stroke-dasharray":"4 2"})
        mod.scale_paper_strokes(g, zoom)
        self.assertAlmostEqual(float(g.attrib["stroke-width"]) * zoom, 0.50, places=6)
        values = [float(x) for x in g.attrib["stroke-dasharray"].split()]
        self.assertAlmostEqual(values[0] * zoom, 4.0, places=6)
        self.assertAlmostEqual(values[1] * zoom, 2.0, places=6)


if __name__ == "__main__":
    unittest.main()
