#!/usr/bin/env python3
"""Deterministic block, road-network and frontage topology helpers.

All coordinates must be in a projected metric CRS. The module is intentionally geometry
only: it never fetches data or writes plan deliverables.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass

import networkx as nx
from shapely import make_valid
from shapely.geometry import GeometryCollection, LineString, MultiPoint, Point, Polygon
from shapely.ops import polygonize, split, unary_union
from shapely.strtree import STRtree


@dataclass
class BlockTopology:
    footprints: list[Polygon]
    shared_walls: list[LineString]
    faces: list[Polygon]
    adjacency: nx.Graph
    courtyard_widths_m: dict[int, tuple[float, ...]]
    diagnostics: list[str]


def _line_parts(geom) -> list[LineString]:
    if geom is None or geom.is_empty:
        return []
    if isinstance(geom, LineString):
        return [geom]
    if isinstance(geom, GeometryCollection) or hasattr(geom, "geoms"):
        return [part for child in geom.geoms for part in _line_parts(child)]
    return []


def _polygon_parts(geom) -> list[Polygon]:
    if geom is None or geom.is_empty:
        return []
    if isinstance(geom, Polygon):
        return [geom]
    if hasattr(geom, "geoms"):
        return [part for child in geom.geoms for part in _polygon_parts(child)]
    return []


def courtyard_widths(poly: Polygon) -> tuple[float, ...]:
    """Return each patio's short MRR side in metres; never prune sub-2m rings."""
    out = []
    for ring in poly.interiors:
        void = Polygon(ring)
        coords = list(void.minimum_rotated_rectangle.exterior.coords)
        lengths = [math.dist(a, b) for a, b in zip(coords, coords[1:], strict=False) if math.dist(a, b) > 1e-9]
        out.append(min(lengths) if lengths else 0.0)
    return tuple(out)


def _edge_records(polygons: list[Polygon]):
    records = []
    for pid, poly in enumerate(polygons):
        coords = list(poly.exterior.coords)
        vertices = coords[:-1] if len(coords) > 1 and coords[0] == coords[-1] else coords
        n = len(vertices)
        for idx in range(n):
            a, b = vertices[idx], vertices[(idx + 1) % n]
            if math.dist(a, b) > 1e-8:
                records.append((pid, idx, n, a, b, LineString([a, b])))
    return records


def snap_shared_walls(
    polygons: Iterable[Polygon],
    max_gap_m: float = 0.8,
    angle_tolerance_deg: float = 5.0,
    min_shared_length_m: float = 1.0,
    max_area_change: float = 0.05,
) -> BlockTopology:
    """Midline-snap near-coextensive party edges, then extract planar faces.

    Matching is restricted to nearly parallel, substantially coextensive exterior edges;
    non-matching features remain unchanged. Endpoints on each accepted pair receive the
    exact same target coordinates. Courtyard rings are never edited or removed.
    """
    source = list(polygons)
    diagnostics: list[str] = []
    if not source:
        return BlockTopology([], [], [], nx.Graph(), {}, [])
    if any(not p.is_valid for p in source):
        diagnostics.append("Invalid input footprint(s); block snap is conservative")
    records = _edge_records(source)
    lines = [r[-1] for r in records]
    tree = STRtree(lines) if lines else None
    updates: dict[tuple[int, int], list[tuple[float, float]]] = {}
    tol_angle = math.radians(angle_tolerance_deg)
    matched_pairs: set[tuple[int, int]] = set()
    for i, rec in enumerate(records):
        pid, idx, n, a, b, line = rec
        if tree is None:
            break
        for j0 in tree.query(line.buffer(max_gap_m + 1e-9)):
            j = int(j0)
            if j <= i:
                continue
            other = records[j]
            qid, qidx, qn, c, d, other_line = other
            if pid == qid or line.distance(other_line) > max_gap_m + 1e-9:
                continue
            ux, uy = b[0] - a[0], b[1] - a[1]
            vx, vy = d[0] - c[0], d[1] - c[1]
            la, lb = math.hypot(ux, uy), math.hypot(vx, vy)
            ux, uy, vx, vy = ux / la, uy / la, vx / lb, vy / lb
            dot = ux * vx + uy * vy
            if abs(dot) < math.cos(tol_angle):
                continue
            if dot < 0:
                vx, vy = -vx, -vy
            sx, sy = ux + vx, uy + vy
            norm = math.hypot(sx, sy)
            if norm < 1e-8:
                continue
            ux, uy = sx / norm, sy / norm
            nxv, nyv = -uy, ux
            ta = sorted((a[0] * ux + a[1] * uy, b[0] * ux + b[1] * uy))
            tb = sorted((c[0] * ux + c[1] * uy, d[0] * ux + d[1] * uy))
            lo, hi = max(ta[0], tb[0]), min(ta[1], tb[1])
            overlap = hi - lo
            if overlap < min_shared_length_m or overlap < 0.95 * min(la, lb):
                continue
            if max(abs(ta[0] - tb[0]), abs(ta[1] - tb[1])) > max_gap_m * 1.5:
                continue
            mid_a = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
            mid_b = ((c[0] + d[0]) / 2, (c[1] + d[1]) / 2)
            da = mid_a[0] * nxv + mid_a[1] * nyv
            db = mid_b[0] * nxv + mid_b[1] * nyv
            if abs(da - db) > max_gap_m + 1e-9:
                continue
            offset = (da + db) / 2.0
            target_lo = (lo * ux + offset * nxv, lo * uy + offset * nyv)
            target_hi = (hi * ux + offset * nxv, hi * uy + offset * nyv)
            for edge in (rec, other):
                ep_pid, eidx, en, ep0, ep1, _ = edge
                start_target, end_target = (
                    (target_lo, target_hi)
                    if ep0[0] * ux + ep0[1] * uy <= ep1[0] * ux + ep1[1] * uy
                    else (target_hi, target_lo)
                )
                updates.setdefault((ep_pid, eidx), []).append(start_target)
                updates.setdefault((ep_pid, (eidx + 1) % en), []).append(end_target)
            matched_pairs.add(tuple(sorted((pid, qid))))

    moved = []
    for pid, poly in enumerate(source):
        coords = list(poly.exterior.coords)
        vertices = coords[:-1] if len(coords) > 1 and coords[0] == coords[-1] else coords
        updated = []
        for idx, point in enumerate(vertices):
            targets = updates.get((pid, idx), [])
            if targets:
                updated.append((sum(p[0] for p in targets) / len(targets), sum(p[1] for p in targets) / len(targets)))
            else:
                updated.append((float(point[0]), float(point[1])))
        candidate = Polygon(updated, [list(r.coords) for r in poly.interiors])
        relative = abs(candidate.area - poly.area) / max(poly.area, 1e-9)
        if not candidate.is_valid or len(candidate.interiors) != len(poly.interiors) or relative > max_area_change:
            diagnostics.append(f"Footprint {pid}: shared-wall correction rejected; source retained")
            moved.append(poly)
        else:
            moved.append(candidate)

    shared_walls = []
    adjacency = nx.Graph()
    adjacency.add_nodes_from(range(len(moved)))
    for pid in range(len(moved)):
        for qid in range(pid + 1, len(moved)):
            common = moved[pid].boundary.intersection(moved[qid].boundary)
            shared = [line for line in _line_parts(common) if line.length >= min_shared_length_m]
            if shared:
                adjacency.add_edge(pid, qid, shared_length_m=sum(line.length for line in shared))
                shared_walls.extend(shared)
    linework = unary_union([poly.boundary for poly in moved])
    faces = list(polygonize(linework))
    building_union = unary_union(moved)
    faces = [face for face in faces if face.area > 1e-8 and face.intersection(building_union).area / face.area >= 0.999]
    widths = {i: courtyard_widths(poly) for i, poly in enumerate(moved) if poly.interiors}
    for i, values in widths.items():
        if any(width < 2.0 for width in values):
            diagnostics.append(f"Footprint {i}: sub-2m patio retained and flagged: {values}")
    return BlockTopology(moved, shared_walls, faces, adjacency, widths, diagnostics)


def _intersection_points(geom) -> list[Point]:
    if geom is None or geom.is_empty:
        return []
    if isinstance(geom, Point):
        return [geom]
    if isinstance(geom, LineString):
        coords = list(geom.coords)
        return [Point(coords[0]), Point(coords[-1])] if len(coords) >= 2 else []
    if hasattr(geom, "geoms"):
        return [point for child in geom.geoms for point in _intersection_points(child)]
    return []


def _truthy_osm_tag(value) -> bool:
    return str(value).strip().lower() in {"yes", "true", "1", "bridge", "viaduct", "culvert"}


def build_road_graph(segments: Iterable[tuple], node_precision: int = 6) -> nx.MultiGraph:
    """Node centerlines at same-level intersections; retain width, source and level."""
    normalized = []
    for item in segments:
        line, width = item[0], float(item[1])
        metadata = item[2] if len(item) > 2 and isinstance(item[2], dict) else {}
        if line is not None and not line.is_empty and line.length > 1e-6 and width > 0:
            level = (
                str(metadata.get("layer", "0")),
                _truthy_osm_tag(metadata.get("bridge", False)),
                _truthy_osm_tag(metadata.get("tunnel", False)),
            )
            normalized.append((line, width, level, metadata))
    graph = nx.MultiGraph()
    if not normalized:
        return graph
    lines = [item[0] for item in normalized]
    tree = STRtree(lines)
    cuts = [[Point(line.coords[0]), Point(line.coords[-1])] for line in lines]
    for i, line in enumerate(lines):
        for j0 in tree.query(line):
            j = int(j0)
            if j <= i or normalized[i][2] != normalized[j][2]:
                continue
            for point in _intersection_points(line.intersection(lines[j])):
                cuts[i].append(point)
                cuts[j].append(point)
    for i, (line, width, level, metadata) in enumerate(normalized):
        unique = {}
        for point in cuts[i]:
            unique[(round(point.x, node_precision), round(point.y, node_precision))] = point
        splitter = MultiPoint(list(unique.values()))
        try:
            parts = [
                part for part in split(line, splitter).geoms if isinstance(part, LineString) and part.length > 1e-6
            ]
        except Exception:
            parts = [line]
        for part in parts:
            p0, p1 = part.coords[0], part.coords[-1]
            u = (round(float(p0[0]), node_precision), round(float(p0[1]), node_precision), level)
            v = (round(float(p1[0]), node_precision), round(float(p1[1]), node_precision), level)
            if u == v:
                continue
            graph.add_node(u, x=float(p0[0]), y=float(p0[1]), level=level)
            graph.add_node(v, x=float(p1[0]), y=float(p1[1]), level=level)
            graph.add_edge(
                u, v, geometry=part, width_m=width, level=level, source=metadata.get("source", "unknown"), tags=metadata
            )
    return graph


def road_corridor_from_graph(
    graph: nx.MultiGraph, quad_segs: int = 12, join_style: str = "round", mitre_limit: float = 2.0
):
    """Buffer graph edges, add round node transition patches, then dissolve/validate."""
    if graph.number_of_edges() == 0:
        return GeometryCollection()
    surfaces = []
    for _, _, _, data in graph.edges(keys=True, data=True):
        geom = data["geometry"]
        width = data["width_m"]
        surfaces.append(
            geom.buffer(
                width / 2.0, cap_style="flat", join_style=join_style, mitre_limit=mitre_limit, quad_segs=quad_segs
            )
        )
    for node, data in graph.nodes(data=True):
        degree = graph.degree(node)
        if degree < 2:
            continue  # true dead-end gets its explicit flat terminal
        widths = [edge[3]["width_m"] for edge in graph.edges(node, keys=True, data=True)]
        radius = max(widths) / 2.0
        surfaces.append(Point(data["x"], data["y"]).buffer(radius, quad_segs=quad_segs))
    merged = make_valid(unary_union(surfaces))
    if not merged.is_valid:
        merged = make_valid(merged)
    return merged


def reconcile_parcel_frontages(
    parcel_lines: Iterable[LineString], road_surface, tolerance_m: float = 0.35
) -> tuple[list[LineString], int]:
    """Project only near-curb parcel vertices to the public-domain curb skeleton."""
    from shapely.ops import nearest_points

    curb = road_surface.boundary if not road_surface.is_empty else GeometryCollection()
    if curb.is_empty:
        return list(parcel_lines), 0
    out, snapped_count = [], 0
    for line in parcel_lines:
        if line.is_empty or line.length <= 1e-8:
            continue
        coords = list(line.coords)
        adjusted = []
        changed = False
        for x, y, *_ in coords:
            point = Point(x, y)
            if point.distance(curb) <= tolerance_m:
                projected, _ = nearest_points(curb, point)
                adjusted.append((projected.x, projected.y))
                changed = True
            else:
                adjusted.append((x, y))
        candidate = LineString(adjusted)
        if changed and candidate.is_valid and candidate.length > 1e-6:
            out.append(candidate)
            snapped_count += 1
        else:
            out.append(line)
    return out, snapped_count
