#!/usr/bin/env python3
"""Build a cadastral Fond de Plan from OSM, Microsoft footprints and a reference image.

Metric model coordinates are retained in the selected UTM zone. SVG/PDF are paper-space
sheets at a declared print scale. No raster edge detection or external tile imagery is used.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import urllib.parse
import urllib.request
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

import ezdxf
import geopandas as gpd
import osmnx as ox
import pandas as pd
import requests
import svgwrite
from deliverable_qa import DRAWING_DELIVERABLES, qa_output_directory
from footprint_providers import (
    OSM_ATTRIBUTION,
    BaseFootprintProvider,
    DeduplicationReport,
    FootprintRecord,
    MicrosoftFootprintProvider,
    get_footprint_provider,
    merge_footprint_records,
    threshold_provenance_report,
)
from layout_engine import add_paperspace_viewport, metadata_from_utm, resolve_layout, resolve_sheet_set
from PIL import Image
from pyproj import Transformer
from report_schema import apply_detail, input_provenance, provenance_block
from scipy.spatial import cKDTree
from shapely import make_valid, minimum_rotated_rectangle
from shapely.affinity import rotate
from shapely.affinity import scale as scale_affine
from shapely.geometry import GeometryCollection, LineString, MultiLineString, MultiPolygon, Point, Polygon
from shapely.ops import transform
from urban_topology import build_road_graph, reconcile_parcel_frontages, road_corridor_from_graph, snap_shared_walls

OSM_API = "https://api.openstreetmap.org/api/0.6/map"
ENGINE_VERSION = "1.7.0"
DEFAULT_REPORT_DETAIL = "full"

# Architectural trace digitized from the supplied image after matching Microsoft building
# polygons. Pixel coordinates are the original 1106 x 1280 JPEG. Roads are centerlines,
# widths are measured/estimated from visible pavement; these are not cadastral survey lines.
PHOTO_ROADS = [
    (
        [
            (0, 379),
            (45, 367),
            (92, 354),
            (145, 346),
            (205, 343),
            (266, 345),
            (326, 351),
            (390, 352),
            (460, 349),
            (535, 346),
            (604, 342),
            (664, 341),
            (703, 355),
            (726, 377),
        ],
        5.0,
    ),
    (
        [
            (340, 352),
            (328, 370),
            (319, 394),
            (316, 430),
            (316, 485),
            (317, 545),
            (315, 612),
            (318, 683),
            (326, 735),
            (346, 774),
        ],
        4.8,
    ),
    (
        [
            (42, 394),
            (39, 447),
            (40, 503),
            (44, 558),
            (47, 620),
            (45, 684),
            (50, 744),
            (56, 799),
            (61, 864),
            (65, 940),
            (68, 1030),
            (70, 1120),
            (73, 1279),
        ],
        4.6,
    ),
    (
        [
            (346, 773),
            (383, 785),
            (429, 797),
            (477, 808),
            (528, 819),
            (580, 829),
            (631, 839),
            (674, 852),
            (704, 876),
            (713, 912),
            (715, 960),
            (719, 1013),
            (727, 1072),
        ],
        5.0,
    ),
    (
        [
            (721, 379),
            (739, 405),
            (753, 438),
            (767, 476),
            (781, 518),
            (794, 560),
            (805, 603),
            (813, 644),
            (814, 678),
            (807, 709),
            (794, 735),
            (776, 756),
            (753, 775),
            (728, 799),
            (708, 822),
            (697, 850),
        ],
        4.6,
    ),
    ([(0, 798), (47, 792), (92, 789), (139, 786), (184, 787), (225, 792), (270, 795), (314, 779)], 4.4),
    ([(702, 1130), (770, 1122), (843, 1119), (922, 1125), (1003, 1138), (1080, 1151), (1105, 1156)], 4.5),
]
PHOTO_PARCELS = [
    [(19, 0), (34, 78), (45, 158), (57, 234), (70, 294), (89, 351)],
    [(178, 0), (188, 82), (197, 160), (207, 237), (218, 313)],
    [(438, 0), (452, 83), (467, 163), (483, 238), (500, 304)],
    [(500, 0), (510, 86), (523, 165), (536, 238), (548, 318)],
    [(216, 315), (273, 317), (340, 320), (405, 321), (474, 318), (534, 322)],
    [(624, 283), (660, 285), (699, 278), (741, 261)],
]
# Inner edge of the road-ringed vacant plot, interpreted directly from the supplied aerial.
SITE_PIXELS = [
    (340, 414),
    (339, 390),
    (347, 375),
    (362, 363),
    (389, 358),
    (454, 357),
    (525, 359),
    (597, 360),
    (654, 365),
    (690, 376),
    (711, 393),
    (720, 423),
    (728, 462),
    (740, 505),
    (751, 550),
    (762, 594),
    (772, 637),
    (783, 675),
    (786, 708),
    (776, 732),
    (756, 749),
    (727, 762),
    (690, 769),
    (649, 766),
    (602, 759),
    (551, 751),
    (502, 746),
    (452, 745),
    (408, 753),
    (371, 762),
    (351, 758),
    (341, 741),
    (336, 711),
    (334, 670),
    (334, 621),
    (334, 571),
    (335, 523),
    (336, 476),
    (338, 440),
]


def utm_epsg(lat: float, lon: float) -> int:
    zone = max(1, min(60, int(math.floor((lon + 180.0) / 6.0)) + 1))
    return (32600 if lat >= 0 else 32700) + zone


def resolve_maps_url(url: str) -> tuple[float, float, str]:
    r = requests.get(url, allow_redirects=True, timeout=45, headers={"User-Agent": "cad-fond-de-plan/1.0"})
    final = r.url
    candidates = [final, urllib.parse.unquote(final)]
    for candidate in candidates:
        for pattern in (
            r"[?&](?:q|query)=(-?\d+(?:\.\d+)?)[,%20]+(-?\d+(?:\.\d+)?)",
            r"/@(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)",
            r"!3d(-?\d+(?:\.\d+)?)!4d(-?\d+(?:\.\d+)?)",
        ):
            m = re.search(pattern, candidate)
            if m:
                return float(m.group(1)), float(m.group(2)), final
    raise ValueError(f"Could not resolve a coordinate pair from final map URL: {final}")


def quadkey(lat: float, lon: float, zoom: int = 9) -> str:
    xtile = int((lon + 180.0) / 360.0 * (1 << zoom))
    lat_rad = math.radians(max(-85.05112878, min(85.05112878, lat)))
    ytile = int((1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * (1 << zoom))
    out = []
    for i in range(zoom, 0, -1):
        mask = 1 << (i - 1)
        out.append(str((1 if xtile & mask else 0) + (2 if ytile & mask else 0)))
    return "".join(out)


def flatten_polygons(geom) -> list[Polygon]:
    if geom is None or geom.is_empty:
        return []
    if isinstance(geom, Polygon):
        return [geom]
    if isinstance(geom, (MultiPolygon, GeometryCollection)):
        out = []
        for part in geom.geoms:
            out.extend(flatten_polygons(part))
        return out
    return []


def flatten_lines(geom) -> list[LineString]:
    if geom is None or geom.is_empty:
        return []
    if isinstance(geom, LineString):
        return [geom]
    if isinstance(geom, (MultiLineString, GeometryCollection)):
        out = []
        for part in geom.geoms:
            out.extend(flatten_lines(part))
        return out
    return []


def acquire_osm(lat: float, lon: float, radius: float, out_dir: Path, local_file: Path | None):
    tags = {"building": True, "highway": True, "landuse": True, "barrier": True, "natural": True}
    if local_file:
        if local_file.suffix.lower() == ".gpkg":
            gdf = gpd.read_file(local_file)
            if gdf.crs is None:
                gdf = gdf.set_crs(4326)
            source = f"local OSM GeoPackage: {local_file}"
            method = "local-gpkg"
        else:
            gdf = ox.features_from_xml(local_file, tags=tags)
            if local_file.name == "osm-map.osm":
                source = f"OpenStreetMap API map bbox extract, cached XML: {local_file}"
                method = "osm-api-map-bbox-cached"
            else:
                source = f"local OSM XML extract: {local_file}"
                method = "local-xml"
        return gdf, source, method

    # Prefer Overpass via OSMnx. If public endpoints are unavailable, use the small
    # OpenStreetMap API bbox extract and spatially clip it in projected coordinates.
    try:
        ox.settings.timeout = 120
        ox.settings.requests_timeout = 120
        ox.settings.use_cache = True
        ox.settings.cache_folder = str(out_dir / "cache")
        ox.settings.log_console = False
        gdf = ox.features_from_point((lat, lon), tags=tags, dist=radius)
        return gdf, "OpenStreetMap Overpass API via OSMnx", "overpass"
    except Exception as overpass_error:
        zone = utm_epsg(lat, lon)
        to_utm = Transformer.from_crs(4326, zone, always_xy=True)
        circle_utm = Point(*to_utm.transform(lon, lat)).buffer(radius, resolution=48)
        circle_wgs = transform(Transformer.from_crs(zone, 4326, always_xy=True).transform, circle_utm)
        minx, miny, maxx, maxy = circle_wgs.bounds
        url = f"{OSM_API}?bbox={minx:.8f},{miny:.8f},{maxx:.8f},{maxy:.8f}"
        response = requests.get(url, timeout=120, headers={"User-Agent": "cad-fond-de-plan/1.0 (OSM vector extract)"})
        response.raise_for_status()
        xml_path = out_dir / "source" / "osm-api-map.osm"
        xml_path.parent.mkdir(parents=True, exist_ok=True)
        xml_path.write_bytes(response.content)
        gdf = ox.features_from_xml(xml_path, tags=tags)
        return (
            gdf,
            f"OpenStreetMap API map bbox fallback; Overpass error: {type(overpass_error).__name__}",
            "osm-api-map-bbox",
        )


def acquire_microsoft(lat: float, lon: float, out_dir: Path, local_file: Path | None):
    """Backward-compatible Microsoft acquisition wrapper."""
    paths, source = MicrosoftFootprintProvider().acquire(lat, lon, 0.0, out_dir, local_file)
    return paths[0], source


def read_microsoft(path: Path, to_utm: Transformer, study_circle: Polygon) -> list[Polygon]:
    """Backward-compatible geometry-only reader; new code should retain records."""
    records = MicrosoftFootprintProvider().read([path], to_utm, study_circle)
    return [record.geometry for record in records]


def axial_mean(angles: Iterable[float], weights: Iterable[float] | None = None) -> float:
    vals = list(angles)
    if not vals:
        return 0.0
    ws = list(weights) if weights is not None else [1.0] * len(vals)
    if len(ws) != len(vals) or not any(w > 0 for w in ws):
        return vals[0]
    x = sum(w * math.cos(2 * a) for a, w in zip(vals, ws, strict=True))
    y = sum(w * math.sin(2 * a) for a, w in zip(vals, ws, strict=True))
    if math.hypot(x, y) < 1e-10:
        return vals[0]
    return 0.5 * math.atan2(y, x)


def axial_difference(a: float, b: float) -> float:
    """Smallest unsigned angle between undirected axes, in radians."""
    return abs((a - b + math.pi / 2.0) % math.pi - math.pi / 2.0)


def rect_axis(poly: Polygon) -> tuple[float, float]:
    """Return the dominant MRR edge angle and long/short side ratio.

    A rectangle's two longest edges are opposite and equal, so aspect must use the
    shortest non-degenerate edge, not the second-longest edge.
    """
    rect = minimum_rotated_rectangle(poly)
    coords = list(rect.exterior.coords)
    edges = []
    for a, b in zip(coords, coords[1:], strict=False):
        dx, dy = b[0] - a[0], b[1] - a[1]
        length = math.hypot(dx, dy)
        if length > 1e-9:
            edges.append((length, math.atan2(dy, dx)))
    if not edges:
        return 0.0, 1.0
    major_edge = max(edges, key=lambda edge: edge[0])
    minor = min(length for length, _ in edges)
    return major_edge[1] % math.pi, major_edge[0] / max(minor, 1e-9)


def clean_ring(coords: list[tuple[float, float]], min_segment: float) -> list[tuple[float, float]]:
    pts = [(float(x), float(y)) for x, y, *_ in coords]
    if len(pts) > 1 and math.dist(pts[0], pts[-1]) < 1e-8:
        pts.pop()
    changed = True
    while changed and len(pts) > 4:
        changed = False
        for i, p in enumerate(pts):
            prev, nxt = pts[(i - 1) % len(pts)], pts[(i + 1) % len(pts)]
            if math.dist(prev, p) < min_segment or math.dist(p, nxt) < min_segment:
                del pts[i]
                changed = True
                break
    return pts


class _UF:
    def __init__(self, n: int):
        self.p = list(range(n))

    def find(self, x: int) -> int:
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a: int, b: int):
        a, b = self.find(a), self.find(b)
        if a != b:
            self.p[b] = a


def orthogonal_ring(coords, axis: float, min_segment: float) -> list[tuple[float, float]]:
    # Rotate into the local cluster frame, remove micro-vertices, then solve exact
    # equal-coordinate constraints so every output segment is exactly horizontal/vertical.
    world = clean_ring(list(coords), min_segment)
    if len(world) < 4:
        world = [(float(x), float(y)) for x, y, *_ in coords[:-1]]
    c, s = math.cos(axis), math.sin(axis)
    local = [(c * x + s * y, -s * x + c * y) for x, y in world]
    n = len(local)
    if n < 4:
        return world
    ux, uy = _UF(n), _UF(n)
    for i in range(n):
        j = (i + 1) % n
        dx, dy = local[j][0] - local[i][0], local[j][1] - local[i][1]
        if abs(dx) >= abs(dy):
            uy.union(i, j)  # horizontal edge: equal local y
        else:
            ux.union(i, j)  # vertical edge: equal local x
    xgroups: dict[int, list[float]] = {}
    ygroups: dict[int, list[float]] = {}
    for i, (x, y) in enumerate(local):
        xgroups.setdefault(ux.find(i), []).append(x)
        ygroups.setdefault(uy.find(i), []).append(y)
    ox = {k: sum(v) / len(v) for k, v in xgroups.items()}
    oy = {k: sum(v) / len(v) for k, v in ygroups.items()}
    orth = [(ox[ux.find(i)], oy[uy.find(i)]) for i in range(n)]
    return [(c * x - s * y, s * x + c * y) for x, y in orth]


def orthogonalize(poly: Polygon, axis: float, min_segment: float = 1.2) -> tuple[Polygon, bool]:
    """Snap rings to a dominant local axis while preserving area, aspect and courtyards.

    If constrained snapping would require more than 25% linear correction or would alter
    ring topology, retain the valid source geometry and flag the conservative fallback.
    This deliberately avoids replacing an elongated/concave building with its MRR.
    """
    if poly.is_empty:
        return poly, True
    source = poly
    if not source.is_valid:
        repaired = make_valid(source)
        if not isinstance(repaired, Polygon) or not repaired.is_valid:
            # Keep every repaired component rather than silently selecting the largest.
            return repaired, True
        source = repaired
    if source.area <= 1e-8:
        return source, True

    try:
        exterior = orthogonal_ring(list(source.exterior.coords), axis, min_segment)
        holes = [orthogonal_ring(list(r.coords), axis, min_segment) for r in source.interiors]
        candidate = make_valid(Polygon(exterior, holes))
        if not isinstance(candidate, Polygon) or not candidate.is_valid:
            return source, True
        if len(candidate.interiors) != len(source.interiors):
            return source, True
        if candidate.area <= 1e-8:
            return source, True

        # Preserve the source MRR long/short ratio, orienting its long side to the
        # nearest of the two orthogonal directions represented by `axis`.
        source_axis, target_aspect = rect_axis(source)
        relative = axial_difference(source_axis, axis)
        target_ratio = target_aspect if relative <= math.pi / 4.0 else 1.0 / target_aspect
        local = rotate(candidate, -math.degrees(axis), origin="centroid", use_radians=False)
        minx, miny, maxx, maxy = local.bounds
        width, height = maxx - minx, maxy - miny
        if width <= 1e-8 or height <= 1e-8:
            return source, True
        current_ratio = width / height

        # Independent local-axis factors solve both constraints simultaneously:
        # sx*sy = A_source/A_snapped; (width*sx)/(height*sy) = target_ratio.
        area_factor = source.area / candidate.area
        ratio_factor = target_ratio / current_ratio
        sx = math.sqrt(area_factor * ratio_factor)
        sy = math.sqrt(area_factor / ratio_factor)
        if not all(math.isfinite(v) and 0.8 <= v <= 1.25 for v in (sx, sy)):
            return source, True
        if math.isclose(sx, 1.0, rel_tol=0.0, abs_tol=1e-12) and math.isclose(sy, 1.0, rel_tol=0.0, abs_tol=1e-12):
            corrected = candidate  # avoid needless transform roundoff on coincident party walls
        else:
            corrected_local = scale_affine(local, xfact=sx, yfact=sy, origin="centroid")
            corrected = rotate(corrected_local, math.degrees(axis), origin="centroid", use_radians=False)
        if not isinstance(corrected, Polygon) or not corrected.is_valid:
            return source, True
        if len(corrected.interiors) != len(source.interiors):
            return source, True

        area_error = abs(corrected.area - source.area) / source.area
        final_local = rotate(corrected, -math.degrees(axis), origin="centroid", use_radians=False)
        fx0, fy0, fx1, fy1 = final_local.bounds
        final_ratio = (fx1 - fx0) / max(fy1 - fy0, 1e-8)
        aspect_error = abs(final_ratio - target_ratio) / target_ratio
        source_holes = sorted(Polygon(r).area for r in source.interiors)
        final_holes = sorted(Polygon(r).area for r in corrected.interiors)
        hole_errors = [
            abs(after - before) / max(before, 1e-8) for before, after in zip(source_holes, final_holes, strict=False)
        ]
        if area_error > 0.05 or aspect_error > 0.05 or any(err > 0.05 for err in hole_errors):
            return source, True
        return corrected, False
    except Exception:
        # Safer to retain the source than to distort a footprint into a rectangle.
        return source, True


def building_axes(polys: list[Polygon], radius_m: float = 35.0, cluster_tolerance_deg: float = 15.0) -> list[float]:
    """Estimate dominant axes without averaging distinct street/building directions.

    Elongated footprints contribute a confidence-weighted axial mean only to a nearby
    orientation mode. Near-square footprints inherit the nearest local mode; if no
    reliable mode exists, their own MRR angle is returned with low confidence implicit.
    """
    if not polys:
        return []
    centers = [(p.centroid.x, p.centroid.y) for p in polys]
    tree = cKDTree(centers)
    raw = [rect_axis(p) for p in polys]
    tolerance = math.radians(cluster_tolerance_deg)
    axes = []
    for i, (theta, aspect) in enumerate(raw):
        neighbors = tree.query_ball_point(centers[i], radius_m)
        elongated = [j for j in neighbors if raw[j][1] >= 1.12]
        if aspect >= 1.12:
            # A reliable footprint keeps its own mode; only nearby aligned forms smooth it.
            cluster = [j for j in elongated if axial_difference(raw[j][0], theta) <= tolerance]
            if i not in cluster:
                cluster.append(i)
        elif elongated:
            # A near-square MRR has an unstable angle. Borrow the nearest strong mode,
            # but do not average it together with a perpendicular/oblique street grid.
            seed = min(elongated, key=lambda j: math.dist(centers[i], centers[j]))
            seed_angle = raw[seed][0]
            cluster = [j for j in elongated if axial_difference(raw[j][0], seed_angle) <= tolerance]
        else:
            axes.append(theta)
            continue
        angles = [raw[j][0] for j in cluster]
        weights = [min(4.0, max(1.0, raw[j][1])) for j in cluster]
        axes.append(axial_mean(angles, weights))
    return axes


def pixel_to_world(
    pix: tuple[float, float], cx: float, cy: float, anchor: tuple[float, float], gsd: float
) -> tuple[float, float]:
    x, y = pix
    return cx + (x - anchor[0]) * gsd, cy - (y - anchor[1]) * gsd


def image_lines(cx, cy, anchor, gsd) -> tuple[list[tuple[LineString, float]], list[LineString], Polygon]:
    roads = []
    for pts, width in PHOTO_ROADS:
        line = LineString([pixel_to_world(p, cx, cy, anchor, gsd) for p in pts])
        roads.append((line, width))
    parcels = [LineString([pixel_to_world(p, cx, cy, anchor, gsd) for p in pts]) for pts in PHOTO_PARCELS]
    site = Polygon([pixel_to_world(p, cx, cy, anchor, gsd) for p in SITE_PIXELS])
    if not site.is_valid:
        site = make_valid(site)
    return roads, parcels, site


def road_width(tags: dict) -> float:
    highway = str(tags.get("highway", "residential"))
    widths = {
        "motorway": 12.0,
        "trunk": 9.0,
        "primary": 8.0,
        "secondary": 6.5,
        "tertiary": 5.5,
        "unclassified": 5.0,
        "residential": 4.8,
        "living_street": 4.0,
        "service": 3.4,
        "pedestrian": 3.0,
        "footway": 1.6,
        "path": 1.3,
        "track": 3.0,
    }
    width_tag = tags.get("width")
    if width_tag:
        m = re.search(r"(\d+(?:\.\d+)?)", str(width_tag))
        if m:
            return max(1.5, min(18.0, float(m.group(1))))
    return widths.get(highway, 4.8)


def road_casements(
    osm: gpd.GeoDataFrame,
    to_utm: Transformer,
    study_circle: Polygon,
    cx: float,
    cy: float,
    manual_roads: list[tuple[LineString, float]],
    return_graph: bool = False,
):
    """Build a same-level, noded centerline graph, then synthesize curb surfaces.

    The default 3-tuple return is kept for existing callers. Set ``return_graph`` to
    additionally receive the graph for DXF centerline/block annotation output.
    """
    local_cut = Point(cx, cy).buffer(82.0)
    segments = []
    osm_count = manual_count = 0
    source_crs = osm.crs.to_epsg() if osm.crs is not None else None
    for geom, tags in zip(osm.geometry, osm.to_dict("records"), strict=True):
        if geom is None or not tags.get("highway"):
            continue
        projected = transform(to_utm.transform, geom) if source_crs == 4326 else geom
        # OSM centerlines are inaccurate in the locally imaged block; use the explicit
        # aerial trace in the inner 82 m, retain OSM context outside it.
        pieces = projected.difference(local_cut)
        metadata = {k: tags.get(k) for k in ("highway", "layer", "bridge", "tunnel", "name") if tags.get(k) is not None}
        metadata["source"] = "OSM"
        for line in flatten_lines(pieces):
            if line.length < 8:
                continue
            for part in flatten_lines(line.intersection(study_circle)):
                segments.append((part, road_width(tags), metadata))
                osm_count += 1
    for line, width in manual_roads:
        for part in flatten_lines(line.intersection(study_circle)):
            if part.length > 2:
                segments.append((part, width, {"source": "reference-aerial", "highway": "local"}))
                manual_count += 1
    graph = build_road_graph(segments)
    corridor = (
        road_corridor_from_graph(graph).intersection(study_circle) if graph.number_of_edges() else GeometryCollection()
    )
    if return_graph:
        return corridor, osm_count, manual_count, graph
    return corridor, osm_count, manual_count


def extract_parcels(
    osm: gpd.GeoDataFrame, to_utm: Transformer, study_circle: Polygon, manual: list[LineString], cx: float, cy: float
) -> list[LineString]:
    lines = list(manual)
    local_cut = Point(cx, cy).buffer(82.0)
    for geom, tags in zip(osm.geometry, osm.to_dict("records"), strict=True):
        if geom is None:
            continue
        if tags.get("landuse") or tags.get("barrier") or tags.get("natural"):
            projected = transform(to_utm.transform, geom) if osm.crs.to_epsg() == 4326 else geom
            if projected.geom_type in ("Polygon", "MultiPolygon"):
                projected = projected.boundary
            clipped = projected.intersection(study_circle).difference(local_cut)
            lines.extend(flatten_lines(clipped))
    clean = []
    for line in lines:
        if line.length > 2.0:
            clean.append(line)
    return clean


def line_hatches(poly: Polygon, spacing_m: float) -> list[LineString]:
    minx, miny, maxx, maxy = poly.bounds
    cs = [y - x for x, y in list(poly.exterior.coords)]
    for ring in poly.interiors:
        cs.extend(y - x for x, y in list(ring.coords))
    step = spacing_m * math.sqrt(2.0)
    start = math.floor(min(cs) / step) * step
    end = max(cs)
    extent = math.hypot(maxx - minx, maxy - miny) + 10.0
    lines = []
    c = start
    while c <= end + step:
        probe = LineString([(minx - extent, minx - extent + c), (maxx + extent, maxx + extent + c)])
        inter = poly.intersection(probe)
        lines.extend([seg for seg in flatten_lines(inter) if seg.length > 0.03])
        c += step
    return lines


def mm_point(x: float, y: float, center: tuple[float, float], scale_mm_m: float) -> tuple[float, float]:
    cx, cy = center
    return (x - cx + 300.0) * scale_mm_m, (300.0 - (y - cy)) * scale_mm_m


def svg_path_line(geom, center, scale) -> str:
    parts = flatten_lines(geom)
    commands = []
    for line in parts:
        pts = [mm_point(x, y, center, scale) for x, y in line.coords]
        if not pts:
            continue
        commands.append(f"M {pts[0][0]:.4f},{pts[0][1]:.4f} " + " ".join(f"L {x:.4f},{y:.4f}" for x, y in pts[1:]))
    return " ".join(commands)


def svg_path_polygon(poly: Polygon, center, scale) -> str:
    commands = []
    for ring in [poly.exterior, *poly.interiors]:
        pts = [mm_point(x, y, center, scale) for x, y in ring.coords]
        commands.append(
            f"M {pts[0][0]:.4f},{pts[0][1]:.4f} " + " ".join(f"L {x:.4f},{y:.4f}" for x, y in pts[1:]) + " Z"
        )
    return " ".join(commands)


def write_svg(
    path: Path,
    buildings: list[Polygon],
    road_boundaries,
    parcels: list[LineString],
    site: Polygon,
    hatch_geoms: list[LineString],
    center: tuple[float, float],
    sheet_mm: float,
    scale_mm_m: float,
    hatched: bool,
):
    dwg = svgwrite.Drawing(
        str(path), size=(f"{sheet_mm}mm", f"{sheet_mm}mm"), viewBox=f"0 0 {sheet_mm} {sheet_mm}", profile="full"
    )
    dwg.add(dwg.rect(insert=(0, 0), size=(sheet_mm, sheet_mm), fill="#FFFFFF"))
    g_roads = dwg.g(
        id="ROADS", fill="none", stroke="#000000", stroke_width="0.35", stroke_linecap="butt", stroke_linejoin="round"
    )
    p = svg_path_line(road_boundaries, center, scale_mm_m)
    if p:
        g_roads.add(dwg.path(d=p))
    dwg.add(g_roads)
    g_parcels = dwg.g(
        id="PARCELS", fill="none", stroke="#000000", stroke_width="0.18", stroke_linecap="butt", stroke_linejoin="miter"
    )
    for line in parcels:
        d = svg_path_line(line, center, scale_mm_m)
        if d:
            g_parcels.add(dwg.path(d=d))
    dwg.add(g_parcels)
    g_build = dwg.g(
        id="BUILDINGS",
        fill="#FFFFFF",
        stroke="#000000",
        stroke_width="0.50",
        fill_rule="evenodd",
        stroke_linejoin="miter",
    )
    for poly in buildings:
        g_build.add(dwg.path(d=svg_path_polygon(poly, center, scale_mm_m)))
    dwg.add(g_build)
    if hatched:
        g_hatch = dwg.g(id="HATCH", fill="none", stroke="#000000", stroke_width="0.13", stroke_linecap="butt")
        for line in hatch_geoms:
            d = svg_path_line(line, center, scale_mm_m)
            if d:
                g_hatch.add(dwg.path(d=d))
        dwg.add(g_hatch)
        # Redraw walls over the clipped hatch to preserve a crisp architectural perimeter.
        g_outer = dwg.g(
            id="BUILDING_WALLS_OVER_HATCH", fill="none", stroke="#000000", stroke_width="0.50", stroke_linejoin="miter"
        )
        for poly in buildings:
            g_outer.add(dwg.path(d=svg_path_polygon(poly, center, scale_mm_m)))
        dwg.add(g_outer)
    if not site.is_empty:
        g_site = dwg.g(
            id="SITE_BOUNDARY",
            fill="none",
            stroke="#000000",
            stroke_width="0.60",
            stroke_dasharray="4 2",
            stroke_linecap="butt",
            stroke_linejoin="round",
        )
        g_site.add(dwg.path(d=svg_path_polygon(site, center, scale_mm_m)))
        dwg.add(g_site)
    dwg.save(pretty=True)


def add_dxf_polyline(msp, ring, close: bool, layer: str):
    pts = list(ring.coords)
    if len(pts) > 1 and math.dist(pts[0][:2], pts[-1][:2]) < 1e-6:
        pts = pts[:-1]
    if len(pts) >= 2:
        msp.add_lwpolyline([(float(x), float(y)) for x, y, *_ in pts], close=close, dxfattribs={"layer": layer})


def write_dxf(
    path: Path,
    buildings: list[Polygon],
    road_boundaries,
    parcels: list[LineString],
    site: Polygon,
    hatch_geoms: list[LineString],
    epsg: int,
    center: tuple[float, float],
    scale_mm_m: float,
    road_centerlines=None,
    shared_walls=None,
    layout_bounds_m=None,
    layout_spec: tuple[str, int] | None = None,
):
    """Write a metric R2018 model plus optional exact-scale multi-sheet paper layouts."""
    doc = ezdxf.new("R2018", setup=True)
    doc.header["$INSUNITS"] = 6  # meters
    doc.header["$MEASUREMENT"] = 1
    doc.header["$LUNITS"] = 2
    doc.header["$LUPREC"] = 3
    doc.header["$USERI1"] = int(epsg)
    doc.header["$UCSORG"] = (center[0], center[1], 0)

    # Keep legacy layer names for downstream consumers; new G-* layers are the
    # controlled architectural schema consumed by the precision export.
    layers = {
        "BUILDINGS": {"color": 7, "lineweight": 50},
        "HATCH": {"color": 7, "lineweight": 13},
        "ROADS": {"color": 7, "lineweight": 35},
        "PARCELS": {"color": 7, "lineweight": 18},
        "SITE_BOUNDARY": {"color": 7, "lineweight": 60, "linetype": "SITE_DASH"},
        "G-BLDG-WALL": {"color": 7, "lineweight": 50},
        "G-BLDG-SHARED": {"color": 7, "lineweight": 50},
        "G-ROAD-CURB": {"color": 7, "lineweight": 35},
        "G-ROAD-CL": {"color": 7, "lineweight": 18, "linetype": "CENTER"},
        "G-PARCEL": {"color": 7, "lineweight": 18},
        "G-HATCH": {"color": 7, "lineweight": 13},
        "G-SITE-BOUNDARY": {"color": 7, "lineweight": 60, "linetype": "SITE_DASH"},
        "G-ANNO-GRID": {"color": 7, "lineweight": 13},
        "G-ANNO-NORTH": {"color": 7, "lineweight": 25},
        "G-ANNO-TTLB": {"color": 7, "lineweight": 35},
    }
    if "SITE_DASH" not in doc.linetypes:
        doc.linetypes.add(
            "SITE_DASH", pattern=[4.0 / scale_mm_m, -2.0 / scale_mm_m], description="4mm dash / 2mm gap at sheet scale"
        )
    if "CENTER" not in doc.linetypes:
        doc.linetypes.add("CENTER", pattern=[1.25, -0.25, 0.25, -0.25], description="Road centerline")
    for name, attrs in layers.items():
        if name not in doc.layers:
            doc.layers.new(name, dxfattribs=attrs)
    doc.header["$CLAYER"] = "G-BLDG-WALL"
    msp = doc.modelspace()
    for line in flatten_lines(road_boundaries):
        add_dxf_polyline(msp, line, False, "G-ROAD-CURB")
    for line in parcels:
        add_dxf_polyline(msp, line, False, "G-PARCEL")
    for poly in buildings:
        add_dxf_polyline(msp, poly.exterior, True, "G-BLDG-WALL")
        for ring in poly.interiors:
            add_dxf_polyline(msp, ring, True, "G-BLDG-WALL")
    for candidate in shared_walls or []:
        for line in flatten_lines(candidate):
            add_dxf_polyline(msp, line, False, "G-BLDG-SHARED")
    for line in hatch_geoms:
        add_dxf_polyline(msp, line, False, "G-HATCH")
    if not site.is_empty:
        add_dxf_polyline(msp, site.exterior, True, "G-SITE-BOUNDARY")
        for ring in site.interiors:
            add_dxf_polyline(msp, ring, True, "G-SITE-BOUNDARY")
    if road_centerlines is not None:
        from layout_engine import ensure_annotation_blocks

        ensure_annotation_blocks(doc)
        for candidate in road_centerlines:
            for line in flatten_lines(candidate):
                add_dxf_polyline(msp, line, False, "G-ROAD-CL")

    if layout_bounds_m is not None:
        layouts = (
            [resolve_layout(tuple(layout_bounds_m), layout_spec[0], layout_spec[1])]
            if layout_spec
            else resolve_sheet_set(tuple(layout_bounds_m))
        )
        area = float(site.area) if site is not None and not site.is_empty else None
        focus_e = (float(layout_bounds_m[0]) + float(layout_bounds_m[2])) / 2.0
        focus_n = (float(layout_bounds_m[1]) + float(layout_bounds_m[3])) / 2.0
        metadata = metadata_from_utm(focus_e, focus_n, epsg, area)
        # Fixed-size UTM grid crosses and labels are composed in each paper layout.
        for sheet_layout in layouts:
            add_paperspace_viewport(doc, sheet_layout, metadata)
    doc.saveas(path)


def render_sheet(hollow_svg: Path, hatched_svg: Path, out_png: Path, out_pdf: Path, sheet_mm: float):
    import cairosvg

    width_px = round(sheet_mm / 25.4 * 300)
    cairosvg.svg2png(
        url=str(hatched_svg), write_to=str(out_png), output_width=width_px, output_height=width_px, dpi=300
    )
    with Image.open(out_png) as im:
        im.save(out_png, dpi=(300, 300), optimize=True)
    cairosvg.svg2pdf(url=str(hatched_svg), write_to=str(out_pdf), dpi=96)


def qa_outputs(
    out_dir: Path,
    building_count: int,
    hatch_count: int,
    epsg: int,
    expected_px: int,
    *,
    expected_layouts: list[str] | None = None,
) -> dict:
    """Run the shared QA contract over the just-written deliverables.

    Delegates to :mod:`deliverable_qa` so the builder, ``tools/check.py`` and the
    MCP ``verify_topology`` tool cannot disagree about the same artifact set.
    The report is not yet written at this point, so layout and pixel
    expectations are supplied explicitly by the caller.
    """
    result = qa_output_directory(
        out_dir,
        include_report=False,
        expected_layouts=frozenset(expected_layouts or ()),
        expected_png_px=expected_px,
    )
    findings = [finding for finding in result["findings"] if not finding.startswith("missing deliverable:")]
    for name in DRAWING_DELIVERABLES:
        candidate = out_dir / name
        if not candidate.exists() or candidate.stat().st_size < 100:
            findings.append(f"missing or tiny deliverable: {name}")
    return {
        "ok": not findings,
        "findings": findings,
        "orthogonalized_buildings": building_count,
        "clipped_hatch_segments": hatch_count,
        "epsg": epsg,
        "qa_contract_version": result.get("qa_contract_version"),
    }


def build_footprint_summary(
    provider: BaseFootprintProvider,
    footprint_source: str,
    footprint_paths: list[Path],
    min_confidence: float | None,
    provider_records: list[FootprintRecord],
    unique_records: list[FootprintRecord],
    ortho_sources: list[FootprintRecord],
    target_precision: int | None = None,
    thresholds_path: Path | None = None,
    thresholds_source: str = "not-requested",
    dedup_report: DeduplicationReport | None = None,
) -> dict:
    """Assemble report-ready footprint provenance, including threshold policy.

    Kept free of I/O so the report contract can be regression-tested offline.
    """
    confidence_scores = [record.confidence for record in provider_records if record.confidence is not None]
    source_counts: dict[str, int] = {}
    for record in unique_records:
        source_counts[record.provider] = source_counts.get(record.provider, 0) + 1
    building_provenance = [record.provenance(index) for index, record in enumerate(ortho_sources)]
    return {
        "provider": provider.name,
        "source": footprint_source,
        "source_files": [str(path) for path in footprint_paths],
        "attribution": provider.attribution,
        "capabilities": provider.capabilities.to_dict(),
        "confidence_threshold": min_confidence,
        **threshold_provenance_report(provider_records, target_precision, thresholds_path, thresholds_source),
        "source_records_loaded": len(provider_records),
        "source_confidence_mean": (sum(confidence_scores) / len(confidence_scores) if confidence_scores else None),
        "source_confidence_min": min(confidence_scores) if confidence_scores else None,
        "source_confidence_max": max(confidence_scores) if confidence_scores else None,
        "unique_input_footprints_after_osm_merge": len(unique_records),
        "unique_input_counts_by_provider": source_counts,
        "deduplication": dedup_report.to_dict() if dedup_report is not None else None,
        "final_output_footprints": len(building_provenance),
        "output_provenance": building_provenance,
    }


def regularize_footprint_records(records: list[FootprintRecord], site: Polygon, study_circle: Polygon):
    """Run inferred/OSM provider records through normalization and block topology.

    Returned lineage is index-aligned with ``BlockTopology.footprints`` so each final
    geometry can retain its source ID and score in the report.
    """
    axes = building_axes([record.geometry for record in records])
    ortho_raw: list[Polygon] = []
    output_sources: list[FootprintRecord] = []
    conservative_fallbacks = vacant_site_false_positives = 0
    for record, axis in zip(records, axes, strict=True):
        poly = record.geometry
        if not site.is_empty and poly.intersection(site).area / max(poly.area, 1e-8) > 0.08:
            vacant_site_false_positives += 1
            continue
        regular, used_fallback = orthogonalize(poly, axis, 1.2)
        if not regular.is_empty and regular.area >= 4.0:
            clipped = regular.intersection(study_circle)
            output_parts = [part for part in flatten_polygons(clipped) if part.area >= 3.0]
            ortho_raw.extend(output_parts)
            output_sources.extend([record] * len(output_parts))
            conservative_fallbacks += int(used_fallback)
    topology = snap_shared_walls(ortho_raw, max_gap_m=0.8, max_area_change=0.05)
    return topology, output_sources, conservative_fallbacks, vacant_site_false_positives


def build(args) -> dict:
    generated_at_utc = datetime.now(UTC).isoformat(timespec="seconds")
    out_dir = Path(args.output).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "source").mkdir(exist_ok=True)
    if args.maps_url:
        lat, lon, resolved_url = resolve_maps_url(args.maps_url)
    else:
        lat, lon, resolved_url = args.lat, args.lon, "coordinates supplied directly"
    if lat is None or lon is None:
        raise ValueError("Supply --maps-url or both --lat and --lon")
    epsg = utm_epsg(lat, lon)
    to_utm = Transformer.from_crs(4326, epsg, always_xy=True)
    cx, cy = to_utm.transform(lon, lat)
    center = Point(cx, cy)
    radius = float(args.radius)
    study_circle = center.buffer(radius, resolution=96)
    image_path = Path(args.image).expanduser() if args.image else None
    if image_path and not image_path.exists():
        raise FileNotFoundError(f"reference image not found: {image_path}")
    anchor = (float(args.anchor_x), float(args.anchor_y))
    gsd = float(args.gsd)
    if image_path:
        with Image.open(image_path) as img:
            photo_size = img.size
        if photo_size != (1106, 1280):
            raise ValueError(
                f"configured photo control frame expects 1106x1280 pixels, received {photo_size}; provide correct georeferencing controls"
            )
        manual_roads, manual_parcels, site = image_lines(cx, cy, anchor, gsd)
        site = site.intersection(study_circle)
        if not isinstance(site, Polygon):
            site_parts = flatten_polygons(site)
            site = max(site_parts, key=lambda p: p.area)
    else:
        manual_roads, manual_parcels, site = [], [], Polygon()

    local_osm_file = Path(args.osm_file).expanduser() if args.osm_file else None
    osm, osm_source, osm_method = acquire_osm(lat, lon, radius, out_dir, local_osm_file)
    if osm.crs is None:
        osm = osm.set_crs(4326)
    osm = osm[osm.geometry.notna()].copy()
    if osm.crs.to_epsg() != epsg:
        osm_metric = osm.to_crs(epsg)
    else:
        osm_metric = osm.copy()
    osm_metric["geometry"] = osm_metric.geometry.intersection(study_circle)
    osm_metric = osm_metric[~osm_metric.geometry.is_empty].copy()

    provider_name = getattr(args, "footprint_provider", "microsoft")
    provider: BaseFootprintProvider = get_footprint_provider(
        provider_name, getattr(args, "google_max_download_mb", 250.0)
    )
    thresholds_path = None
    thresholds_source = "not-requested"
    target_precision = None
    if provider_name == "google-open-buildings":
        local_footprint_file = getattr(args, "google_open_buildings_file", None)
        min_confidence = getattr(args, "google_min_confidence", provider.default_min_confidence)
        target_precision = getattr(args, "google_target_precision", None)
        requested_thresholds = getattr(args, "google_thresholds_path", None)
    else:
        local_footprint_file = getattr(args, "microsoft_file", None)
        min_confidence = None
        requested_thresholds = None
    if local_footprint_file:
        local_footprint_file = Path(local_footprint_file).expanduser()
    footprint_paths, footprint_source = provider.acquire(lat, lon, radius, out_dir, local_footprint_file)
    if provider_name == "google-open-buildings" and target_precision is not None:
        thresholds_path, thresholds_source = provider.acquire_thresholds(
            out_dir, Path(requested_thresholds).expanduser() if requested_thresholds else None
        )
        provider_records = provider.read(
            footprint_paths,
            to_utm,
            study_circle,
            min_confidence,
            target_precision=target_precision,
            thresholds_path=thresholds_path,
            thresholds_source=thresholds_source,
        )
    else:
        provider_records = provider.read(footprint_paths, to_utm, study_circle, min_confidence)

    osm_records: list[FootprintRecord] = []
    for row_index, row in osm_metric.iterrows():
        if pd.notna(row.get("building")):
            osm_id = str(row_index)
            osm_records.extend(
                FootprintRecord(
                    polygon,
                    "osm",
                    f"osm:{osm_id}",
                    None,
                    "ODbL-1.0",
                    {"osm_id": osm_id},
                )
                for polygon in flatten_polygons(row.geometry)
                if polygon.area >= 4.0
            )
    # OSM retains priority over near-identical inferred outlines; all records retain
    # their original source IDs/confidence through deduplication and topology.
    dedup_report = DeduplicationReport()
    unique_records = merge_footprint_records(osm_records, provider_records, report=dedup_report)
    block_topology, ortho_sources, conservative_fallbacks, vacant_site_false_positives = regularize_footprint_records(
        unique_records, site, study_circle
    )
    buildings = block_topology.footprints

    # Road casements: node aerial/OSM centerlines at same-level junctions, synthesize
    # curb surfaces from the graph, and reconcile only near-curb parcel frontage vertices.
    road_casement, osm_road_count, photo_road_count, road_graph = road_casements(
        osm, to_utm, study_circle, cx, cy, manual_roads, return_graph=True
    )
    road_boundaries = road_casement.boundary if not road_casement.is_empty else GeometryCollection()
    road_centerlines = [data["geometry"] for _, _, _, data in road_graph.edges(keys=True, data=True)]
    parcels = extract_parcels(osm, to_utm, study_circle, manual_parcels, cx, cy)
    parcels, frontage_snaps = reconcile_parcel_frontages(parcels, road_casement, tolerance_m=0.35)

    sheet_mm = float(args.sheet_mm)
    scale_mm_m = sheet_mm / (2 * radius)
    # Requested 3.5 px hatch pitch at 96 DPI, converted to world metres at declared plot scale.
    hatch_pitch_mm = 3.5 / (96.0 / 25.4)
    hatch_spacing_m = hatch_pitch_mm / scale_mm_m
    hatch_geoms = []
    for poly in buildings:
        hatch_geoms.extend(line_hatches(poly, hatch_spacing_m))
    hollow = out_dir / "fond-de-plan-hollow.svg"
    hatched = out_dir / "fond-de-plan-hatched.svg"
    write_svg(hollow, buildings, road_boundaries, parcels, site, [], (cx, cy), sheet_mm, scale_mm_m, False)
    write_svg(hatched, buildings, road_boundaries, parcels, site, hatch_geoms, (cx, cy), sheet_mm, scale_mm_m, True)
    layout_bounds_m = (
        tuple(site.bounds)
        if not site.is_empty
        else tuple(study_circle.bounds)
        if getattr(args, "layout_sheet", None)
        else None
    )
    layout_spec = None
    if getattr(args, "layout_sheet", None):
        default_scales = {"A3": 250, "A1": 500, "A0": 1000}
        layout_spec = (args.layout_sheet, args.layout_scale or default_scales[args.layout_sheet])
    write_dxf(
        out_dir / "fond-de-plan-cad.dxf",
        buildings,
        road_boundaries,
        parcels,
        site,
        hatch_geoms,
        epsg,
        (cx, cy),
        scale_mm_m,
        road_centerlines=road_centerlines,
        shared_walls=block_topology.shared_walls,
        layout_bounds_m=layout_bounds_m,
        layout_spec=layout_spec,
    )
    png_path, pdf_path = out_dir / "fond-de-plan-sheet-300dpi.png", out_dir / "fond-de-plan-sheet-300dpi.pdf"
    render_sheet(hollow, hatched, png_path, pdf_path, sheet_mm)
    expected_px = round(sheet_mm / 25.4 * 300)
    layout_sheets = (
        [f"{layout_spec[0]} 1:{layout_spec[1]}"]
        if layout_spec
        else ["A3 1:250", "A1 1:500", "A0 1:1000"]
        if layout_bounds_m
        else []
    )
    expected_layout_names = [label.replace(" ", "-").replace(":", "_") for label in layout_sheets]
    qa = qa_outputs(
        out_dir,
        len(buildings),
        len(hatch_geoms),
        epsg,
        expected_px,
        expected_layouts=expected_layout_names,
    )
    footprint_summary = build_footprint_summary(
        provider,
        footprint_source,
        footprint_paths,
        min_confidence,
        provider_records,
        unique_records,
        ortho_sources,
        target_precision,
        thresholds_path,
        thresholds_source,
        dedup_report,
    )
    legacy_provider_report = {}
    if provider_name == "microsoft":
        legacy_provider_report["microsoft"] = {
            "source": footprint_source,
            "tile": footprint_paths[0].name,
            "footprints_intersecting_300m_before_dedup": len(provider_records),
            "merged_unique_input_footprints": len(unique_records),
        }
    attribution = f"{OSM_ATTRIBUTION}; {provider.attribution}; reference aerial supplied by user."
    threshold_warnings = []
    if target_precision is not None:
        threshold_warnings.append(
            f"Google Open Buildings S2-cell confidence thresholds target an estimated {target_precision}% precision "
            "tier; this is a dataset-published estimate, not a measured or guaranteed accuracy for this site, and "
            "cells without a published value fall back to the standard minimum confidence cutoff."
        )

    report = {
        "ok": qa["ok"],
        "findings": qa["findings"],
        "center": {"lat": lat, "lon": lon, "utm_x_m": cx, "utm_y_m": cy, "epsg": epsg, "zone": (epsg % 100)},
        "google_maps_resolved_url": resolved_url,
        "radius_m": radius,
        "scale": f"1:{(2 * radius * 1000 / sheet_mm):.1f}",
        "photo_georeference": {
            "path": str(image_path) if image_path else None,
            "anchor_pixel": list(anchor),
            "anchor_coordinate": [lat, lon],
            "gsd_m_per_pixel": gsd,
            "rotation_deg": 0.0,
            "method": f"north-up scale/translation visually registered against {provider.name} footprints; not a cadastral survey",
        },
        "osm": {
            "source": osm_source,
            "method": osm_method,
            "raw_features_in_300m": len(osm_metric),
            "roads_used_outside_local_photo_block": osm_road_count,
            "photo_traced_road_centerlines": photo_road_count,
            "source_building_polygons": len(osm_records),
            "landuse_barrier_natural_features": int(
                sum(
                    any(pd.notna(r.get(k)) and str(r.get(k)).strip() for k in ("landuse", "barrier", "natural"))
                    for r in osm_metric.to_dict("records")
                )
            ),
        },
        "footprints": footprint_summary,
        **legacy_provider_report,
        "building_regularization": {
            "algorithm": "dominant MRR axis with 15-degree axial neighborhood modes; local x/y correction conserves source area and aspect within 5%; candidate shared-wall midline snap rejects invalid, hole-changing, or >5% area changes",
            "output_buildings": len(buildings),
            "conservative_source_fallbacks": conservative_fallbacks,
            "false_positives_removed_inside_vacant_site": vacant_site_false_positives,
            "hatch_spacing_world_m": hatch_spacing_m,
            "hatch_segments": len(hatch_geoms),
        },
        "block_topology": {
            "shared_wall_segments": len(block_topology.shared_walls),
            "adjacent_footprint_pairs": block_topology.adjacency.number_of_edges(),
            "polygonized_block_faces": len(block_topology.faces),
            "courtyard_widths_m": {str(k): list(v) for k, v in block_topology.courtyard_widths_m.items()},
            "diagnostics": block_topology.diagnostics,
        },
        "roads": {
            "method": "same-level noded centerline graph; flat-ended edge buffers plus junction patches; corridor dissolved and clipped to study area",
            "graph_nodes": road_graph.number_of_nodes(),
            "graph_edges": road_graph.number_of_edges(),
            "frontage_lines_reconciled": frontage_snaps,
            "manual_photo_roads": photo_road_count,
            "osm_road_parts": osm_road_count,
        },
        "paper_layouts": {
            "sheets": layout_sheets,
            "layout_bounds_m": list(layout_bounds_m) if layout_bounds_m else None,
            "cartouche_mm": [180, 50],
            "grid_spacing_m": "50 (A3/A1), 100 (A0)",
        },
        "site_boundary": {
            "source": "manual vector trace from supplied aerial; inferred parcel edge where vegetation obscures curb",
            "area_m2": round(site.area, 1) if not site.is_empty else None,
        },
        "outputs": {
            n: str(out_dir / n)
            for n in [
                "fond-de-plan-hollow.svg",
                "fond-de-plan-hatched.svg",
                "fond-de-plan-cad.dxf",
                "fond-de-plan-sheet-300dpi.png",
                "fond-de-plan-sheet-300dpi.pdf",
                "fond-de-plan-report.json",
            ]
        }
        | {"sheet_mm": sheet_mm, "expected_png_px": expected_px},
        "provenance": provenance_block(
            engine_version=ENGINE_VERSION,
            generated_at=generated_at_utc,
            inputs=[
                *input_provenance(footprint_paths),
                *input_provenance([thresholds_path] if thresholds_path else [], role="score_thresholds_csv"),
                *input_provenance([local_osm_file] if local_osm_file else [], role="osm_source"),
            ],
            parameters={
                "footprint_provider": provider.name,
                "google_min_confidence": min_confidence,
                "google_target_precision": target_precision,
                "radius_m": radius,
                "sheet_mm": sheet_mm,
                "layout_sheets": layout_sheets,
                "epsg": epsg,
            },
        ),
        "qa": qa,
        "attribution": attribution,
        "warnings": [
            f"Reference JPEG has no EXIF geotags/scale; image georeferencing is an estimated {gsd:g} m/px alignment to the map pin and {provider.name} footprints, not survey-grade.",
            "Public Overpass endpoints were unavailable/empty during the data check; the OpenStreetMap API map-bbox XML extract was used and clipped to the 300 m radius.",
            "The east-side connector of the local road loop is partly interpreted beneath tree/shadow cover; verify against current orthophoto or survey.",
            f"{provider.name} footprints are machine-generated, and all inferred geometry is architecturally orthogonalized; check against a licensed cadastral survey before legal boundary use.",
            *threshold_warnings,
        ],
    }
    report = apply_detail(report, getattr(args, "report_detail", DEFAULT_REPORT_DETAIL))
    (out_dir / "fond-de-plan-report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def build_parser() -> argparse.ArgumentParser:
    """Return the CLI parser; kept separate so flag contracts are testable offline."""
    p = argparse.ArgumentParser(description="Professional metric Fond de Plan export (SVG/DXF/PNG/PDF)")
    p.add_argument("--maps-url", help="Google Maps short URL to resolve")
    p.add_argument("--lat", type=float)
    p.add_argument("--lon", type=float)
    p.add_argument("--radius", type=float, default=300.0)
    p.add_argument("--image", help="attached reference JPEG (1106x1280 for current traced profile)")
    p.add_argument("--anchor-x", type=float, default=493.0, help="photo pixel mapped to maps URL coordinate")
    p.add_argument("--anchor-y", type=float, default=680.0, help="photo pixel mapped to maps URL coordinate")
    p.add_argument("--gsd", type=float, default=0.12, help="estimated ground sample distance, metres per pixel")
    p.add_argument("--osm-file", help="cached OSM XML or GeoPackage; omit to query Overpass then OSM API fallback")
    p.add_argument(
        "--footprint-provider",
        choices=("microsoft", "google-open-buildings"),
        default="microsoft",
        help="building footprint source (Microsoft remains the default)",
    )
    p.add_argument("--microsoft-file", help="cached Microsoft GeoJSONL .csv.gz tile; omit to resolve/download tile")
    p.add_argument(
        "--google-open-buildings-file", help="cached Google Open Buildings v3 CSV/CSV.GZ tile or directory of tiles"
    )
    p.add_argument(
        "--google-min-confidence",
        type=float,
        default=0.65,
        help="baseline/fallback Google score threshold [0,1] (default: 0.65)",
    )
    p.add_argument(
        "--google-target-precision",
        type=int,
        choices=(80, 85, 90),
        default=None,
        help="apply the matching Google S2-cell confidence threshold for estimated precision",
    )
    p.add_argument(
        "--google-thresholds-path",
        help="local/cached Google score-threshold CSV; otherwise it is downloaded and cached when target precision is set",
    )
    p.add_argument(
        "--google-max-download-mb",
        type=float,
        default=250.0,
        help="maximum automatic download size per Google tile; larger tiles require a supplied cache",
    )
    p.add_argument("--output", default=str(Path.home() / "site-plan"))
    p.add_argument("--sheet-mm", type=float, default=420.0)
    p.add_argument("--layout-sheet", choices=("A3", "A1", "A0"), help="add one exact-scale paper-space layout")
    p.add_argument(
        "--layout-scale", type=int, choices=(250, 500, 1000), help="layout denominator; requires --layout-sheet"
    )
    p.add_argument(
        "--report-detail",
        choices=("summary", "full"),
        default=DEFAULT_REPORT_DETAIL,
        help="summary omits per-feature provenance arrays to keep archival reports compact",
    )
    p.add_argument("--json", action="store_true", help="emit JSON report to stdout")
    return p


def main() -> int:
    p = build_parser()
    a = p.parse_args()
    if a.layout_scale and not a.layout_sheet:
        p.error("--layout-scale requires --layout-sheet")
    try:
        result = build(a)
        print(
            json.dumps(result, indent=2)
            if a.json
            else json.dumps(
                {
                    "ok": result["ok"],
                    "output": a.output,
                    "buildings": result["building_regularization"]["output_buildings"],
                    "epsg": result["center"]["epsg"],
                    "findings": result["findings"],
                }
            )
        )
        return 0 if result["ok"] else 1
    except Exception as exc:
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
