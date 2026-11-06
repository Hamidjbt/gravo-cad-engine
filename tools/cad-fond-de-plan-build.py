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
from dxf_standards import (
    HATCH_LAYER,
    HATCH_SOLID,
    create_layer,
    layer_attributes,
    pattern_scale_for,
)
from egress_manager import ArtifactEgress, EgressError, egress_report_block, resolve_sink
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
from layout_engine import (
    add_paperspace_viewport,
    coverage_bounds,
    metadata_from_utm,
    resolve_layout,
    resolve_placement,
    resolve_sheet_set,
)
from PIL import Image
from pyproj import Transformer
from reconciliation import ContainmentPolicy, ReconciliationLog, ReconciliationPolicy
from report_schema import apply_detail, input_provenance, provenance_block
from scipy.spatial import cKDTree
from shapely import make_valid, minimum_rotated_rectangle
from shapely.affinity import rotate
from shapely.affinity import scale as scale_affine
from shapely.geometry import GeometryCollection, LineString, MultiLineString, MultiPolygon, Point, Polygon
from shapely.ops import transform
from urban_topology import build_road_graph, reconcile_parcel_frontages, road_corridor_from_graph, snap_shared_walls

OSM_API = "https://api.openstreetmap.org/api/0.6/map"
ENGINE_VERSION = "1.11.0"
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


def mm_point(
    x: float,
    y: float,
    placement,
    scale_mm_m: float | None = None,
) -> tuple[float, float]:
    """Model metres to millimetres on the declared page.

    The origin comes from the placement, so the mapping follows the page instead of an
    assumed 600 m square. ``scale_mm_m`` is accepted for callers that still pass the old
    scalar; when given it must agree with the placement, because two different scales in
    one drawing is exactly the silent-corruption case this release removes.
    """
    if scale_mm_m is not None and abs(scale_mm_m - placement.mm_per_m) > 1e-9:
        raise ValueError(
            f"scale {scale_mm_m:.9g} mm/m disagrees with declared 1:{placement.scale} ({placement.mm_per_m:.9g} mm/m)"
        )
    ox, oy = placement.origin_mm
    return ox + x * placement.mm_per_m, oy - y * placement.mm_per_m


def svg_path_line(geom, placement) -> str:
    parts = flatten_lines(geom)
    commands = []
    for line in parts:
        pts = [mm_point(x, y, placement) for x, y in line.coords]
        if not pts:
            continue
        commands.append(f"M {pts[0][0]:.4f},{pts[0][1]:.4f} " + " ".join(f"L {x:.4f},{y:.4f}" for x, y in pts[1:]))
    return " ".join(commands)


def svg_path_polygon(poly: Polygon, placement) -> str:
    commands = []
    for ring in [poly.exterior, *poly.interiors]:
        pts = [mm_point(x, y, placement) for x, y in ring.coords]
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
    placement,
    hatched: bool,
):
    """Write one true-to-scale sheet.

    The page is the declared ISO 216 size in millimetres and the ``viewBox`` is the same
    millimetre rectangle, so 1 unit == 1 mm and a plotter reproduces the declared scale
    exactly. Geometry is clipped to the usable area rather than rescaled into it.
    """
    page_w, page_h = placement.page_w_mm, placement.page_h_mm
    dwg = svgwrite.Drawing(
        str(path), size=(f"{page_w}mm", f"{page_h}mm"), viewBox=f"0 0 {page_w} {page_h}", profile="full"
    )
    dwg.add(dwg.rect(insert=(0, 0), size=(page_w, page_h), fill="#FFFFFF"))
    # Clip the map to the usable area: content beyond the margins must be cut, never
    # squeezed, or the plot would silently print at the wrong scale.
    ux, uy, uw, uh = placement.usable_rect_mm
    # svgwrite returns the added element, but defs.add() does not nest for us: the rect
    # must be a child of the clipPath or the url(#USABLE_AREA) reference resolves to
    # nothing and the sheet is silently unclipped.
    clip = dwg.clipPath(id="USABLE_AREA")
    clip.add(dwg.rect(insert=(ux, uy), size=(uw, uh)))
    dwg.defs.add(clip)
    map_group = dwg.g()
    map_group.attribs["clip-path"] = "url(#USABLE_AREA)"
    dwg.add(map_group)
    g_roads = dwg.g(
        id="ROADS", fill="none", stroke="#000000", stroke_width="0.35", stroke_linecap="butt", stroke_linejoin="round"
    )
    p = svg_path_line(road_boundaries, placement)
    if p:
        g_roads.add(dwg.path(d=p))
    map_group.add(g_roads)
    g_parcels = dwg.g(
        id="PARCELS", fill="none", stroke="#000000", stroke_width="0.18", stroke_linecap="butt", stroke_linejoin="miter"
    )
    for line in parcels:
        d = svg_path_line(line, placement)
        if d:
            g_parcels.add(dwg.path(d=d))
    map_group.add(g_parcels)
    g_build = dwg.g(
        id="BUILDINGS",
        fill="#FFFFFF",
        stroke="#000000",
        stroke_width="0.50",
        fill_rule="evenodd",
        stroke_linejoin="miter",
    )
    for poly in buildings:
        g_build.add(dwg.path(d=svg_path_polygon(poly, placement)))
    map_group.add(g_build)
    if hatched:
        g_hatch = dwg.g(id="HATCH", fill="none", stroke="#000000", stroke_width="0.13", stroke_linecap="butt")
        for line in hatch_geoms:
            d = svg_path_line(line, placement)
            if d:
                g_hatch.add(dwg.path(d=d))
        map_group.add(g_hatch)
        # Redraw walls over the clipped hatch to preserve a crisp architectural perimeter.
        g_outer = dwg.g(
            id="BUILDING_WALLS_OVER_HATCH", fill="none", stroke="#000000", stroke_width="0.50", stroke_linejoin="miter"
        )
        for poly in buildings:
            g_outer.add(dwg.path(d=svg_path_polygon(poly, placement)))
        map_group.add(g_outer)
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
        g_site.add(dwg.path(d=svg_path_polygon(site, placement)))
        map_group.add(g_site)
    dwg.save(pretty=True)


#: DXF writers stamp the current time and a fresh GUID into every file, so two runs of
#: the same inputs produce different bytes. That breaks reproducibility and makes the
#: artifact's size — which a resumable upload session must declare up front — vary
#: between runs. Canonicalising these four header timestamps, the ezdxf version stamp
#: and every GUID makes the output byte-identical for identical inputs.
_DXF_TIMESTAMP = re.compile(r"(\$TD(?:UPDATE|CREATE|UUPDATE|UCREATE)\n\s*\d+\n)[0-9.\-]+")
_DXF_EZDXF_STAMP = re.compile(r"(\d+\.\d+\.\d+ @ )[0-9T:.+\-]+")
_DXF_GUID = re.compile(r"\{[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\}")


def canonicalize_dxf_text(text: str) -> str:
    """Replace volatile metadata so identical inputs yield identical bytes."""
    text = _DXF_TIMESTAMP.sub(r"\g<1>2451545.0", text)
    text = _DXF_EZDXF_STAMP.sub(r"\g<1>deterministic", text)
    return _DXF_GUID.sub("{00000000-0000-0000-0000-000000000000}", text)


def save_dxf_deterministic(doc, path: Path) -> None:
    """Write ``doc`` to ``path`` with volatile metadata canonicalised."""
    import io as _io

    buffer = _io.StringIO()
    doc.write(buffer)
    path.write_text(canonicalize_dxf_text(buffer.getvalue()), encoding=doc.output_encoding)


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
    orientation: str = "landscape",
    hatch_pattern: str = HATCH_SOLID,
    road_casement=None,
    min_road_area_m2: float = 1.0,
):
    """Write a metric R2018 model plus optional exact-scale multi-sheet paper layouts.

    v1.11.0 adds plotting styling: ACI-coloured layers with hierarchical lineweights,
    building poché bounded by its courtyards, and closed road polygons where the
    casement geometry allows it. ``hatch_pattern`` is ``SOLID`` (default architectural
    fill) or ``ANSI31``; the pattern scale is derived from the declared denominator so
    the hatch lands at a fixed pitch on paper regardless of plot scale.
    """
    doc = ezdxf.new("R2018", setup=True)
    doc.header["$INSUNITS"] = 6  # meters
    doc.header["$MEASUREMENT"] = 1
    doc.header["$LUNITS"] = 2
    doc.header["$LUPREC"] = 3
    doc.header["$USERI1"] = int(epsg)
    doc.header["$UCSORG"] = (center[0], center[1], 0)

    # v1.11.0: layers, ACI colours and lineweights come from tools/dxf_standards.py so
    # the builder and the QA gate cannot disagree about what a layer should look like.
    layers = layer_attributes()
    # Show lineweights in the editor; without this a correct file looks unstyled.
    doc.header["$LWDISPLAY"] = 1
    if "SITE_DASH" not in doc.linetypes:
        doc.linetypes.add(
            "SITE_DASH", pattern=[4.0 / scale_mm_m, -2.0 / scale_mm_m], description="4mm dash / 2mm gap at sheet scale"
        )
    if "CENTER" not in doc.linetypes:
        doc.linetypes.add("CENTER", pattern=[1.25, -0.25, 0.25, -0.25], description="Road centerline")
    for name in layers:
        if name not in doc.layers:
            create_layer(doc, name)
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

    # v1.11.0 — road surfaces as clean, closed polygons for urban background context.
    # The casement is already a polygonized corridor, so its polygons are the surfaces;
    # only when that polygonization produced nothing do we fall back to open boundary
    # lines, and the caller records that fallback in the report rather than pretending
    # the result is closed.
    road_polygons = 0
    if road_casement is not None and not getattr(road_casement, "is_empty", True):
        for polygon in flatten_polygons(make_valid(road_casement)):
            if polygon.is_empty or polygon.area < min_road_area_m2:
                continue
            add_dxf_polyline(msp, polygon.exterior, True, "G-ROAD-SURFACE")
            for ring in polygon.interiors:
                add_dxf_polyline(msp, ring, True, "G-ROAD-SURFACE")
            road_polygons += 1

    # v1.11.0 — architectural poché. One HATCH per building, with each courtyard as an
    # inner island path so it reads as void. flags=1 marks the outer path external;
    # inner rings must be flags=0 or the courtyard fills in. hatch_style is set
    # explicitly (0 = nested, odd-parity) instead of relying on the library default.
    hatch_scale = pattern_scale_for(layout_spec[1]) if layout_spec else None
    hatched = 0
    for poly in buildings:
        rings = [poly.exterior, *poly.interiors]
        usable = []
        for ring in rings:
            coords = [(float(x), float(y)) for x, y, *_ in ring.coords]
            if len(coords) >= 4:
                usable.append(coords)
        if not usable:
            continue
        hatch = msp.add_hatch(dxfattribs={"layer": HATCH_LAYER})
        if hatch_pattern == HATCH_SOLID:
            hatch.set_solid_fill(color=8)
        else:
            hatch.set_pattern_fill(hatch_pattern, scale=hatch_scale or 1.0)
        hatch.dxf.hatch_style = 0  # nested / odd-parity: islands stay empty
        for index, coords in enumerate(usable):
            hatch.paths.add_polyline_path(coords, is_closed=True, flags=1 if index == 0 else 0)
        hatched += 1

    if layout_bounds_m is not None:
        layouts = (
            [resolve_layout(tuple(layout_bounds_m), layout_spec[0], layout_spec[1], orientation=orientation)]
            if layout_spec
            else resolve_sheet_set(tuple(layout_bounds_m), orientation=orientation)
        )
        area = float(site.area) if site is not None and not site.is_empty else None
        focus_e = (float(layout_bounds_m[0]) + float(layout_bounds_m[2])) / 2.0
        focus_n = (float(layout_bounds_m[1]) + float(layout_bounds_m[3])) / 2.0
        metadata = metadata_from_utm(focus_e, focus_n, epsg, area)
        # Fixed-size UTM grid crosses and labels are composed in each paper layout.
        for sheet_layout in layouts:
            add_paperspace_viewport(doc, sheet_layout, metadata)
    save_dxf_deterministic(doc, path)


def resolve_sheet_placement(args, radius: float, center_utm: tuple[float, float]):
    """Resolve the printed page for this run.

    A requested ``--layout-sheet``/``--layout-scale`` pair is the only statement of a real
    plot scale, so it drives the sheet. Without one, ``--sheet-mm`` names a custom square
    page and the scale is whatever fits the radius — reported honestly as ``custom``
    rather than mislabelled "A3".
    """
    layout_sheet = getattr(args, "layout_sheet", None)
    if layout_sheet:
        default_scales = {"A3": 250, "A1": 500, "A0": 1000}
        scale = int(args.layout_scale or default_scales[layout_sheet])
        placement = resolve_placement(
            coverage_bounds(
                center_utm, _nominal_page(layout_sheet, getattr(args, "orientation", "landscape")), pad_m=radius
            ),
            sheet=layout_sheet,
            scale=scale,
            orientation=getattr(args, "orientation", "landscape"),
        )
        ground_w, ground_h = placement.ground_extent_m
        if 2 * radius > ground_w + 1e-6 or 2 * radius > ground_h + 1e-6:
            raise ValueError(
                f"Geometry {2 * radius:.3f}x{2 * radius:.3f}m does not fit {layout_sheet} at 1:{scale}; "
                "choose a smaller denominator or larger sheet"
            )
        return placement

    side_mm = float(args.sheet_mm)
    scale = max(1, round(2 * radius * 1000.0 / side_mm))
    return resolve_placement(
        (-radius, -radius, radius, radius),
        sheet="custom",
        scale=scale,
        orientation="landscape",
        page_mm_override=(side_mm, side_mm),
        margin_mm=0.0,
    )


def _nominal_page(sheet: str, orientation: str):
    """A placement used only to read the page's ground extent before placing content."""
    return resolve_placement((-1.0, -1.0, 1.0, 1.0), sheet=sheet, scale=1000, orientation=orientation)


def expected_png_px(placement) -> tuple[int, int]:
    """Raster size of the sheet at 300 dpi, per axis.

    Derived from the page itself, so a 420 x 297 mm sheet cannot silently become a
    4961-pixel square.
    """
    return (
        round(placement.page_w_mm / 25.4 * 300),
        round(placement.page_h_mm / 25.4 * 300),
    )


def render_sheet(hollow_svg: Path, hatched_svg: Path, out_png: Path, out_pdf: Path, placement):
    """Rasterise the declared page at 300 dpi, per axis.

    Both output dimensions come from the page, so a rectangular sheet produces a
    rectangular raster and the pixel:millimetre ratio stays uniform on both axes.
    """
    import cairosvg

    width_px, height_px = expected_png_px(placement)
    cairosvg.svg2png(
        url=str(hatched_svg), write_to=str(out_png), output_width=width_px, output_height=height_px, dpi=300
    )
    with Image.open(out_png) as im:
        im.save(out_png, dpi=(300, 300), optimize=True)
    cairosvg.svg2pdf(url=str(hatched_svg), write_to=str(out_pdf), dpi=96)


def _page_aspect_findings(out_dir: Path, page_mm: tuple[float, float]) -> list[str]:
    """Assert the raster and vector sheets carry the declared page's aspect ratio.

    Both axes are checked because a square sheet reported as A3 is the specific defect
    this release fixes: it silently rescales one axis and prints at a scale the drawing
    does not claim.
    """
    findings: list[str] = []
    expected_ratio = page_mm[0] / page_mm[1]
    png = out_dir / "fond-de-plan-sheet-300dpi.png"
    try:
        from PIL import Image

        with Image.open(png) as image:
            width, height = image.size
        ratio = width / height
        if abs(ratio - expected_ratio) > 1e-3:
            findings.append(
                f"{png.name}: aspect ratio {ratio:.4f} does not match the declared page "
                f"{page_mm[0]:g}x{page_mm[1]:g}mm ({expected_ratio:.4f})"
            )
    except Exception as exc:  # noqa: BLE001 - absent/corrupt rasters are reported elsewhere
        findings.append(f"{png.name}: aspect ratio check failed: {type(exc).__name__}: {exc}")
    return findings


def qa_outputs(
    out_dir: Path,
    building_count: int,
    hatch_count: int,
    epsg: int,
    expected_px: tuple[int, int] | int,
    *,
    expected_layouts: list[str] | None = None,
    declared_page_mm: tuple[float, float] | None = None,
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
        # Not `or ()`: an empty declaration is a real expectation (none), not a
        # request to fall back to the legacy A3/A1/A0 set (D3).
        expected_layouts=frozenset(expected_layouts or []),
        expected_png_px=expected_px,
        declared_page_mm=declared_page_mm,
    )
    if declared_page_mm is not None:
        findings_pre = _page_aspect_findings(out_dir, declared_page_mm)
    else:
        findings_pre = []
    findings = [finding for finding in result["findings"] if not finding.startswith("missing deliverable:")]
    findings.extend(findings_pre)
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


def _reconciliation_report(log: ReconciliationLog | None, dedup: DeduplicationReport | None) -> dict | None:
    """Summarise reconciliation, and fail loudly if its counts disagree with dedup.

    A mismatch would mean a drop happened without an auditable record.
    """
    if log is None:
        return None
    summary = log.summary()
    payload = {**summary, "pairs": log.sorted_pairs()}
    if dedup is not None:
        accounted = summary["duplicates_dropped"] + summary["absorbed"] + summary["authoritative_contained_dropped"]
        expected = dedup.dropped_as_duplicate + dedup.dropped_as_authoritative_duplicate
        payload["audit"] = {
            "drops_expected": expected,
            "drops_accounted_with_reason": accounted,
            "consistent": accounted == expected,
        }
    return payload


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
    reconciliation: ReconciliationLog | None = None,
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
        "reconciliation": _reconciliation_report(reconciliation, dedup_report),
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


def build(args, egress: ArtifactEgress | None = None) -> dict:
    """Build the deliverable, routing heavy artifacts through ``egress`` if supplied.

    ``build`` itself never decides where artifacts live. With an egress manager the
    output directory is the manager's scratch buffer, so the monitored tree sees no
    artifact bytes; without one the caller's ``--output`` is used directly.
    """
    if egress is not None:
        return _build_with_egress(args, egress)
    return _build_core(args, Path(args.output).expanduser().resolve())


def _build_with_egress(args, egress: ArtifactEgress) -> dict:
    """Run the build inside an egress context: buffer in, receipt out, nothing else.

    The report is flushed *last* so the copy that lands remotely describes remote
    locations: flushing it first would upload a document pointing at a scratch
    directory that no longer exists by the time anyone reads it.
    """
    out_dir = egress.output_dir("artifacts")
    report = _build_core(args, out_dir)
    milestone = egress.milestone or "artifacts"

    heavy = [name for name in _deliverable_names(out_dir) if name != "fond-de-plan-report.json"]
    for name in heavy:
        egress.register(out_dir / name, logical_name=f"{milestone}/{name}")
    egress.register_tree(out_dir / "source", prefix=f"{milestone}/source/")
    egress.flush()

    report["outputs"] = {name: egress.uri_map().get(f"{milestone}/{name}", "") for name in heavy}
    report["egress"] = egress_report_block(egress)

    report_path = out_dir / "fond-de-plan-report.json"
    if report_path.is_file():
        body = json.dumps(report, indent=2).encode("utf-8")
        if len(body) > REPORT_BLOCK_BYTES:
            raise EgressError(
                f"report is {len(body)} bytes, larger than the {REPORT_BLOCK_BYTES}-byte egress block; "
                "raise REPORT_BLOCK_BYTES so the declared upload size stays predictable"
            )
        # Trailing JSON whitespace is insignificant, so padding is safe for consumers.
        report_path.write_bytes(body + b" " * (REPORT_BLOCK_BYTES - len(body)))
        egress.register(report_path, logical_name=f"{milestone}/fond-de-plan-report.json")
        egress.flush()
        report["outputs"]["fond-de-plan-report.json"] = egress.uri_map().get(
            f"{milestone}/fond-de-plan-report.json", ""
        )
    return report


#: The report is padded to this exact size before egress. A resumable upload session
#: must declare its byte total up front, but the report embeds its own egress block and
#: therefore grows while the run is still describing itself. Padding to a fixed block
#: makes the announced size and the delivered size agree by construction.
REPORT_BLOCK_BYTES = 256 * 1024


def _deliverable_names(out_dir: Path) -> list[str]:
    """Names of the heavy deliverables, in a stable order, that exist on disk."""
    candidates = [
        "fond-de-plan-hollow.svg",
        "fond-de-plan-hatched.svg",
        "fond-de-plan-cad.dxf",
        "fond-de-plan-sheet-300dpi.png",
        "fond-de-plan-sheet-300dpi.pdf",
        "fond-de-plan-report.json",
    ]
    return [name for name in candidates if (out_dir / name).is_file()]


def _build_core(args, out_dir: Path) -> dict:
    generated_at_utc = datetime.now(UTC).isoformat(timespec="seconds")
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
    reconciliation = ReconciliationLog()
    reconciliation_policy = ReconciliationPolicy(
        containment_policy=ContainmentPolicy.coerce(getattr(args, "containment_policy", "nested")),
        containment_threshold=float(getattr(args, "containment_threshold", 0.88)),
        iou_duplicate=float(getattr(args, "iou_duplicate", 0.80)),
        min_child_area_m2=float(getattr(args, "min_child_area_m2", 0.0)),
    )
    unique_records = merge_footprint_records(
        osm_records,
        provider_records,
        report=dedup_report,
        policy=reconciliation_policy,
        reconciliation=reconciliation,
    )
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

    # D5: the printed sheet is resolved through the ISO 216 layout engine, never a
    # square derived from --sheet-mm. A declared --layout-sheet/--layout-scale wins,
    # because that is the only pair that states a real plot scale.
    sheet_placement = resolve_sheet_placement(args, radius, (cx, cy))
    scale_mm_m = sheet_placement.mm_per_m
    # v1.11.0 plotting style. Default SOLID architectural poché; ANSI31 is opt-in.
    hatch_pattern = getattr(args, "hatch_pattern", HATCH_SOLID).upper()
    # Requested 3.5 px hatch pitch at 96 DPI, converted to world metres at declared plot scale.
    hatch_pitch_mm = 3.5 / (96.0 / 25.4)
    hatch_spacing_m = hatch_pitch_mm / scale_mm_m
    hatch_geoms = []
    for poly in buildings:
        hatch_geoms.extend(line_hatches(poly, hatch_spacing_m))
    hollow = out_dir / "fond-de-plan-hollow.svg"
    hatched = out_dir / "fond-de-plan-hatched.svg"
    write_svg(hollow, buildings, road_boundaries, parcels, site, [], sheet_placement, False)
    write_svg(hatched, buildings, road_boundaries, parcels, site, hatch_geoms, sheet_placement, True)
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
        orientation=sheet_placement.orientation,
        hatch_pattern=hatch_pattern,
        road_casement=road_casement,
    )
    # v1.11.0: state honestly whether road casements are closed polygons. Sparse OSM
    # data can leave the corridor empty, in which case the DXF carries open boundary
    # lines and the report says so instead of implying a fill that will not plot.
    dxf_doc = ezdxf.readfile(out_dir / "fond-de-plan-cad.dxf")
    road_surface_count = sum(
        1 for e in dxf_doc.modelspace().query("LWPOLYLINE") if e.dxf.layer == "G-ROAD-SURFACE" and e.closed
    )
    hatch_count = len(list(dxf_doc.modelspace().query("HATCH")))
    road_geometry_state = {
        "mode": "closed-polygons" if road_surface_count else "open-boundaries-fallback",
        "closed_polygons": road_surface_count,
        "fallback": road_surface_count == 0,
        "reason": (
            None
            if road_surface_count
            else "road casement polygonization produced no surface above the area threshold; "
            "open curb boundaries were emitted instead"
        ),
    }
    png_path, pdf_path = out_dir / "fond-de-plan-sheet-300dpi.png", out_dir / "fond-de-plan-sheet-300dpi.pdf"
    render_sheet(hollow, hatched, png_path, pdf_path, sheet_placement)
    expected_px = expected_png_px(sheet_placement)
    layout_sheets = (
        [f"{layout_spec[0]} 1:{layout_spec[1]}"]
        if layout_spec
        else ["A3 1:250", "A1 1:500", "A0 1:1000"]
        if layout_bounds_m
        else []
    )
    expected_layout_names = [label.replace(" ", "-").replace(":", "_") for label in layout_sheets]
    # D3: pass the same declaration the report will carry, so the builder and
    # tools/check.py cannot reach opposite verdicts on the same artifact set. A
    # layout-less run declares no sheets and therefore expects none.
    qa = qa_outputs(
        out_dir,
        len(buildings),
        len(hatch_geoms),
        epsg,
        expected_px,
        expected_layouts=expected_layout_names,
        declared_page_mm=(sheet_placement.page_w_mm, sheet_placement.page_h_mm),
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
        reconciliation,
    )
    legacy_provider_report = {}
    if provider_name == "microsoft":
        legacy_provider_report["microsoft"] = {
            "source": footprint_source,
            "tile": footprint_paths[0].name,
            "footprints_intersecting_300m_before_dedup": len(provider_records),
            "merged_unique_input_footprints": len(unique_records),
        }
    attribution = f"{OSM_ATTRIBUTION}; {provider.attribution}"
    if image_path:
        attribution += "; reference aerial supplied by user."
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
        "scale": f"1:{sheet_placement.scale}",
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
            "sheet": sheet_placement.to_dict(),
            # Does the printed page actually cover the requested radius? A 1:500 page
            # only shows 200 x 138 m of ground; claiming "1:500" for a 600 m site would
            # be a true denominator attached to a false statement about what is on it.
            "sheet_coverage_m": [
                round(sheet_placement.ground_extent_m[0], 3),
                round(sheet_placement.ground_extent_m[1], 3),
            ],
            "requested_extent_m": round(2 * radius, 3),
            "scale_consistent": bool(
                sheet_placement.ground_extent_m[0] + 1e-6 >= 2 * radius
                and sheet_placement.ground_extent_m[1] + 1e-6 >= 2 * radius
            ),
        },
        "site_boundary": {
            # D4: a run without --image has no supplied aerial and no manual trace;
            # saying otherwise in a print deliverable is a false provenance claim.
            "source": (
                "manual vector trace from supplied aerial; inferred parcel edge where vegetation obscures curb"
                if image_path
                else "not traced: no reference aerial supplied, site limited to the study radius"
            ),
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
        | {
            "sheet_mm": sheet_placement.page_w_mm,
            "sheet_mm_secondary": sheet_placement.page_h_mm,
            "expected_png_px": list(expected_px),
            "sheet": sheet_placement.to_dict(),
        },
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
                "sheet_mm": sheet_placement.page_w_mm,
                "sheet_mm_secondary": sheet_placement.page_h_mm,
                "sheet_name": sheet_placement.sheet,
                "orientation": sheet_placement.orientation,
                "layout_sheets": layout_sheets,
                "epsg": epsg,
            },
        ),
        "qa": qa,
        "attribution": attribution,
        "warnings": [*build_warnings(image_path, gsd, provider, osm_method, threshold_warnings)],
    }
    report["dxf_styling"] = {
        "hatch_pattern": hatch_pattern,
        "hatch_scale": None
        if hatch_pattern == HATCH_SOLID
        else round(pattern_scale_for(layout_spec[1]) if layout_spec else 1.0, 3),
        "hatch_entities": hatch_count,
        "hatch_style": "nested (islands empty)",
        "paper_hatch_pitch_mm": 2.0 if hatch_pattern != HATCH_SOLID else None,
        "road_geometry": road_geometry_state,
    }
    report = apply_detail(report, getattr(args, "report_detail", DEFAULT_REPORT_DETAIL))
    (out_dir / "fond-de-plan-report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def build_warnings(
    image_path: Path | None,
    gsd: float,
    provider,
    osm_method: str,
    threshold_warnings: list[str],
) -> list[str]:
    """Warnings that describe what this run actually did.

    D4: every clause is conditional on the event it describes. A warning claiming a
    reference aerial, an Overpass fallback, or a manual road trace that did not
    happen is a false provenance statement in a printed deliverable.
    """
    warnings: list[str] = []
    if image_path:
        warnings.append(
            f"Reference JPEG has no EXIF geotags/scale; image georeferencing is an estimated {gsd:g} m/px "
            f"alignment to the map pin and {provider.name} footprints, not survey-grade."
        )
        warnings.append(
            "Roads and parcels traced from the supplied aerial are visually interpreted; verify against "
            "current orthophoto or survey where cover is partial."
        )
    else:
        warnings.append(
            "No reference aerial supplied: no manually traced roads, parcels or site boundary are present; "
            "geometry is derived from OSM and the building-footprint provider alone."
        )
    if osm_method != "overpass":
        warnings.append(
            "Public Overpass endpoints were unavailable/empty during the data check; the OpenStreetMap API "
            "map-bbox XML extract was used and clipped to the study radius."
        )
    warnings.append(
        f"{provider.name} footprints are machine-generated, and all inferred geometry is architecturally "
        "orthogonalized; check against a licensed cadastral survey before legal boundary use."
    )
    warnings.extend(threshold_warnings)
    return warnings


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
    p.add_argument(
        "--egress",
        choices=("none", "gdrive", "local", "null"),
        default="none",
        help="route heavy artifacts through the egress pipeline instead of --output (default: none)",
    )
    p.add_argument("--egress-milestone", default=None, help="Drive folder label for this run, e.g. v1.10.0")
    p.add_argument("--egress-run-id", default=None, help="stable run identifier; defaults to a UTC timestamp")
    p.add_argument(
        "--egress-receipt-dir",
        default=str(Path.home()),
        help="directory for the single <run_id>_receipt.json (default: the monitored root)",
    )
    p.add_argument(
        "--egress-sessions-json",
        default=None,
        help=(
            "JSON file mapping logical artifact name to a pre-authorized Drive resumable "
            "session URI; used where no in-process Drive credentials exist"
        ),
    )
    p.add_argument(
        "--egress-file-ids-json",
        default=None,
        help="JSON file mapping logical artifact name to its Drive file id, for receipt URIs",
    )
    p.add_argument(
        "--egress-sizes-json",
        default=None,
        help="JSON file mapping logical artifact name to the byte total declared when its session opened",
    )
    p.add_argument(
        "--egress-local-root",
        default=None,
        help="destination directory for --egress local; must be outside the monitored tree",
    )
    p.add_argument("--sheet-mm", type=float, default=420.0)
    p.add_argument("--layout-sheet", choices=("A3", "A1", "A0"), help="add one exact-scale paper-space layout")
    p.add_argument(
        "--orientation",
        choices=("landscape", "portrait"),
        default="landscape",
        help="ISO 216 page orientation for the printed sheet (default: landscape)",
    )
    p.add_argument(
        "--layout-scale", type=int, choices=(250, 500, 1000), help="layout denominator; requires --layout-sheet"
    )
    p.add_argument(
        "--containment-policy",
        choices=("nested", "absorb", "absorb_when_coincident"),
        default="nested",
        help="what to do when one footprint encloses another (default: nested, retain both and report)",
    )
    p.add_argument(
        "--containment-threshold",
        type=float,
        default=0.88,
        help="overlap-of-smaller-footprint needed to call a pair containment (default: 0.88)",
    )
    p.add_argument(
        "--iou-duplicate",
        type=float,
        default=0.80,
        help="intersection-over-union needed to call a pair a duplicate (default: 0.80)",
    )
    p.add_argument(
        "--min-child-area-m2",
        type=float,
        default=0.0,
        help="never absorb a contained footprint smaller than this area (default: 0.0)",
    )
    p.add_argument(
        "--report-detail",
        choices=("summary", "full"),
        default=DEFAULT_REPORT_DETAIL,
        help="summary omits per-feature provenance arrays to keep archival reports compact",
    )
    p.add_argument(
        "--hatch-pattern",
        choices=("solid", "ansi31"),
        default="solid",
        help="building poché fill: solid (default) or ansi31 survey hatching",
    )
    p.add_argument("--json", action="store_true", help="emit JSON report to stdout")
    return p


def _load_json_map(path: str | None) -> dict[str, str]:
    """Load a name->value JSON map, rejecting anything that is not a flat mapping."""
    if not path:
        return {}
    payload = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"expected a JSON object in {path}")
    return {str(key): str(value) for key, value in payload.items()}


def _load_json_int_map(path: str | None) -> dict[str, int]:
    """Load a name->integer map (declared byte totals must stay integers)."""
    if not path:
        return {}
    payload = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"expected a JSON object in {path}")
    return {str(key): int(value) for key, value in payload.items()}


def run_with_egress(args) -> dict:
    """Execute the build inside an ArtifactEgress context and return the report."""
    sink = None
    if args.egress == "local":
        if not args.egress_local_root:
            raise ValueError("--egress local requires --egress-local-root outside the monitored tree")
        sink = resolve_sink("local", local_root=Path(args.egress_local_root).expanduser())
    elif args.egress in {"gdrive", "null"}:
        sink = resolve_sink(
            args.egress,
            sessions=_load_json_map(args.egress_sessions_json),
            file_ids=_load_json_map(args.egress_file_ids_json),
            declared_sizes=_load_json_int_map(args.egress_sizes_json),
        )
    with ArtifactEgress(
        destination=args.egress,
        milestone=args.egress_milestone,
        run_id=args.egress_run_id,
        receipt_dir=Path(args.egress_receipt_dir).expanduser(),
        sink=sink,
        sessions=_load_json_map(args.egress_sessions_json),
        file_ids=_load_json_map(args.egress_file_ids_json),
        declared_sizes=_load_json_int_map(args.egress_sizes_json),
    ) as egress:
        return build(args, egress=egress)


def main() -> int:
    p = build_parser()
    a = p.parse_args()
    if a.layout_scale and not a.layout_sheet:
        p.error("--layout-scale requires --layout-sheet")
    try:
        result = run_with_egress(a) if a.egress != "none" else build(a)
        print(
            json.dumps(result, indent=2)
            if a.json
            else json.dumps(
                {
                    "ok": result["ok"],
                    "output": result.get("outputs", a.output),
                    "buildings": result["building_regularization"]["output_buildings"],
                    "epsg": result["center"]["epsg"],
                    "findings": result["findings"],
                    "egress": result.get("egress"),
                }
            )
        )
        return 0 if result["ok"] else 1
    except EgressError as exc:
        print(json.dumps({"ok": False, "error": f"EgressError: {exc}"}, indent=2))
        return 1
    except Exception as exc:
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
