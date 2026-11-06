"""Provider adapters for building-footprint datasets.

The adapters keep acquisition, parsing, confidence, and lineage metadata separate
from the CAD geometry pipeline. Google Open Buildings uses its official v3 spatially
sharded CSV format; supplying a local file or directory keeps use fully offline.
"""

from __future__ import annotations

import abc
import csv
import gzip
import json
import math
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from acquisition import (
    AcquisitionError,
    AcquisitionLayer,
    RemoteTile,
    SourceCapabilities,
)
from pyproj import Transformer
from shapely import make_valid, wkt
from shapely import transform as shapely_transform
from shapely.errors import ShapelyError
from shapely.geometry import GeometryCollection, MultiPolygon, Point, Polygon, box, shape
from shapely.strtree import STRtree

MS_INDEX = "https://bfppub.blob.core.windows.net/$web/2026-07-24/dataset-links.csv"
OB_TILES_URL = "https://openbuildings-public-dot-gweb-research.uw.r.appspot.com/public/tiles.geojson"
OB_SCORE_THRESHOLDS_URL = "https://storage.googleapis.com/open-buildings-data/v3/score_thresholds_s2_level_4.csv"
THRESHOLDS_FILENAME = "google-open-buildings-score-thresholds.csv"
THRESHOLDS_MAX_MB = 100.0
TILES_INDEX_FILENAME = "google-open-buildings-tiles.geojson"
OB_ATTRIBUTION = "Google Open Buildings v3 (CC BY 4.0 or ODbL 1.0)"
RESOLUTION_BASELINE = "baseline-min-confidence"
RESOLUTION_REGIONAL = "regional-s2-cell-threshold"
RESOLUTION_CELL_MISSING_TIER = "fallback-cell-threshold-missing"
RESOLUTION_NO_CELL_MATCH = "fallback-no-cell-match"
RESOLUTION_TABLE_UNAVAILABLE = "fallback-threshold-table-unavailable"


MS_ATTRIBUTION = "Microsoft Global ML Building Footprints (CDLA-Permissive-2.0)"
OSM_ATTRIBUTION = "© OpenStreetMap contributors (ODbL)"


@dataclass(frozen=True)
class FootprintRecord:
    """A projected footprint plus source lineage retained through CAD processing."""

    geometry: Polygon
    provider: str
    source_id: str
    confidence: float | None = None
    license: str = ""
    attributes: dict[str, Any] = field(default_factory=dict)

    def provenance(self, output_index: int | None = None) -> dict[str, Any]:
        item: dict[str, Any] = {
            "provider": self.provider,
            "source_id": self.source_id,
            "confidence": self.confidence,
            "license": self.license,
        }
        if self.attributes:
            item["attributes"] = self.attributes
        if output_index is not None:
            item["output_index"] = output_index
        return item


class BaseFootprintProvider(abc.ABC):
    """Common provider contract: acquire cached source(s), then parse records."""

    name: str
    attribution: str
    default_min_confidence: float | None = None

    @abc.abstractmethod
    def acquire(
        self,
        lat: float,
        lon: float,
        radius_m: float,
        out_dir: Path,
        local_file: Path | None = None,
    ) -> tuple[list[Path], str]:
        """Return local source paths and a human-readable source description."""

    @abc.abstractmethod
    def read(
        self,
        paths: Iterable[Path],
        to_utm: Transformer,
        study_circle: Polygon,
        min_confidence: float | None = None,
        *,
        target_precision: int | None = None,
        thresholds_path: Path | None = None,
        thresholds_source: str | None = None,
    ) -> list[FootprintRecord]:
        """Parse, project, validate, and spatially clip source features."""


def _flatten_polygons(geometry: Any) -> list[Polygon]:
    if geometry is None or geometry.is_empty:
        return []
    if isinstance(geometry, Polygon):
        return [geometry]
    if isinstance(geometry, (MultiPolygon, GeometryCollection)):
        result: list[Polygon] = []
        for part in geometry.geoms:
            result.extend(_flatten_polygons(part))
        return result
    return []


def _confidence(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        score = float(value)
    except (TypeError, ValueError):
        return None
    return score if math.isfinite(score) and 0.0 <= score <= 1.0 else None


def _project_records(
    geometry: Any,
    source_id: str,
    provider: str,
    license_name: str,
    to_utm: Transformer,
    study_circle: Polygon,
    confidence: float | None = None,
    attributes: dict[str, Any] | None = None,
) -> list[FootprintRecord]:
    if geometry is None or geometry.is_empty:
        return []
    try:
        projected = shapely_transform(geometry, to_utm.transform, interleaved=False)
        if not projected.intersects(study_circle):
            return []
        valid = make_valid(projected.intersection(study_circle))
    except (ShapelyError, TypeError, ValueError):
        return []
    return [
        FootprintRecord(part, provider, source_id, confidence, license_name, attributes or {})
        for part in _flatten_polygons(valid)
        if not part.is_empty and part.area > 0
    ]


def merge_footprint_records(
    authoritative: Iterable[FootprintRecord],
    inferred: Iterable[FootprintRecord],
    *,
    minimum_area_m2: float = 5.0,
    authoritative_overlap: float = 0.62,
    duplicate_overlap: float = 0.88,
) -> list[FootprintRecord]:
    """Prioritize authoritative records and deduplicate inferred/source overlaps.

    OSM building features are passed as ``authoritative``; ML sources (Microsoft or
    Google) are passed as ``inferred``. The geometry and lineage of the retained
    record are kept together, so confidence is never detached from its footprint.
    """
    authoritative_records = [r for r in authoritative if r.geometry.area >= 4.0]
    merged = list(authoritative_records)
    for candidate in inferred:
        if candidate.geometry.area < minimum_area_m2:
            continue
        duplicate = any(
            candidate.geometry.intersection(known.geometry).area
            / max(min(candidate.geometry.area, known.geometry.area), 1e-8)
            > authoritative_overlap
            for known in authoritative_records
        )
        if not duplicate:
            merged.append(candidate)
    merged.sort(key=lambda record: (-record.geometry.area, record.provider, record.source_id))
    unique: list[FootprintRecord] = []
    for candidate in merged:
        duplicate = any(
            candidate.geometry.intersection(previous.geometry).area
            / max(min(candidate.geometry.area, previous.geometry.area), 1e-8)
            > duplicate_overlap
            for previous in unique[:120]
        )
        if not duplicate:
            unique.append(candidate)
    return unique


class MicrosoftFootprintProvider(BaseFootprintProvider):
    """Existing Microsoft Global ML source, retained as the default provider."""

    name = "microsoft"
    attribution = MS_ATTRIBUTION
    capabilities = SourceCapabilities(
        source_id="microsoft",
        display_name="Microsoft Global ML Building Footprints",
        license_id="CDLA-Permissive-2.0",
        supports_remote_discovery=True,
        supports_local_cache=True,
        supports_directory_cache=False,
        supports_size_limit=True,
        supports_confidence_scores=True,
        supports_precision_thresholds=False,
        supports_streaming=True,
        accepted_suffixes=(".csv.gz", ".gz", ".geojsonl", ".csv"),
        notes=(
            "Quadkey-indexed global release; an Algeria location filter is applied.",
            "Compressed GeoJSON-L per line; no per-cell precision tiers published.",
        ),
    )

    def _layer(self, out_dir: Path) -> AcquisitionLayer:
        return AcquisitionLayer(self.capabilities, cache_dir=out_dir / "source")

    def acquire(
        self,
        lat: float,
        lon: float,
        radius_m: float,
        out_dir: Path,
        local_file: Path | None = None,
    ) -> tuple[list[Path], str]:
        del radius_m
        layer = self._layer(out_dir)
        if local_file:
            paths = layer.local_paths(local_file)
            return paths, "local Microsoft footprint tile: " + ", ".join(p.name for p in paths)

        index_path = layer.cached("microsoft-dataset-links.csv") or layer.fetch_index(
            RemoteTile(url=MS_INDEX, filename="microsoft-dataset-links.csv", label="Microsoft dataset index")
        )
        import pandas as pd

        links = pd.read_csv(index_path, dtype={"QuadKey": str})
        target = quadkey(lat, lon, 9)
        matches = links[(links["QuadKey"] == target) & links["Location"].str.lower().eq("algeria")]
        if matches.empty:
            matches = links[
                (
                    links["QuadKey"].astype(str).str.startswith(target)
                    | links["QuadKey"].astype(str).apply(lambda key: target.startswith(key))
                )
                & links["Location"].str.lower().eq("algeria")
            ]
        if matches.empty:
            raise RuntimeError(f"No Microsoft building tile found for quadkey {target}; provide --microsoft-file")
        row = matches.iloc[0]
        tile = RemoteTile(
            url=str(row["Url"]),
            filename=f"microsoft-{row['Location']}-{row['QuadKey']}.csv.gz",
            label=f"quadkey {row['QuadKey']}",
        )
        path = layer.fetch(tile, integrity="gzip")
        return [path], (
            f"Microsoft Global ML Building Footprints, {row['Location']} quadkey {row['QuadKey']} ({row['Url']})"
        )

    def read(
        self,
        paths: Iterable[Path],
        to_utm: Transformer,
        study_circle: Polygon,
        min_confidence: float | None = None,
        *,
        target_precision: int | None = None,
        thresholds_path: Path | None = None,
        thresholds_source: str | None = None,
    ) -> list[FootprintRecord]:
        del min_confidence, target_precision, thresholds_path, thresholds_source
        records: list[FootprintRecord] = []
        for path in paths:
            opener = gzip.open if path.suffix.lower() == ".gz" else open
            with opener(path, "rt", encoding="utf-8") as stream:
                for line_number, line in enumerate(stream, 1):
                    if not line.strip():
                        continue
                    try:
                        feature = json.loads(line)
                        geometry = shape(feature["geometry"])
                        properties = feature.get("properties") or {}
                    except (ShapelyError, json.JSONDecodeError, KeyError, TypeError, ValueError):
                        continue
                    source_id = str(feature.get("id") or properties.get("building_id") or f"{path.stem}:{line_number}")
                    score = _confidence(properties.get("confidence", properties.get("score")))
                    records.extend(
                        _project_records(
                            geometry,
                            source_id,
                            self.name,
                            "CDLA-Permissive-2.0",
                            to_utm,
                            study_circle,
                            score,
                            {key: value for key, value in properties.items() if key in {"type", "class"}},
                        )
                    )
        return records


@dataclass(frozen=True)
class S2ConfidenceThreshold:
    cell_id: str
    geometry: Polygon
    values: dict[int, float | None]


class GoogleScoreThresholdTable:
    """Spatial index over Google's S2-cell precision thresholds (WGS84 polygons)."""

    def __init__(self, cells: list[S2ConfidenceThreshold], source: str):
        self.cells = cells
        self.source = source
        self._geometries = [cell.geometry for cell in cells]
        self._tree = STRtree(self._geometries) if self._geometries else None

    @classmethod
    def from_csv(cls, path: Path, source: str | None = None) -> GoogleScoreThresholdTable:
        required = {
            "s2_token",
            "geometry",
            "confidence_threshold_80%_precision",
            "confidence_threshold_85%_precision",
            "confidence_threshold_90%_precision",
        }
        cells: list[S2ConfidenceThreshold] = []
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            if not reader.fieldnames or not required.issubset(reader.fieldnames):
                raise ValueError(f"Threshold CSV needs columns {sorted(required)}")
            for row in reader:
                try:
                    cell_id = (row.get("s2_token") or "").strip()
                    geometry = wkt.loads(row.get("geometry", ""))
                except (ShapelyError, TypeError, ValueError):
                    continue
                if not cell_id or geometry.is_empty:
                    continue
                values: dict[int, float | None] = {}
                for precision in (80, 85, 90):
                    raw = row.get(f"confidence_threshold_{precision}%_precision")
                    try:
                        value = float(raw) if raw not in (None, "") else math.nan
                    except (TypeError, ValueError):
                        value = math.nan
                    values[precision] = value if math.isfinite(value) and 0 <= value <= 1 else None
                cells.append(S2ConfidenceThreshold(cell_id, geometry, values))
        return cls(cells, source or str(path))

    def resolve(self, point: Point, precision: int) -> tuple[float | None, str | None, str]:
        """Return (threshold, cell id, resolution source) for a WGS84 point."""
        if self._tree is None:
            return None, None, RESOLUTION_NO_CELL_MATCH
        indices = self._tree.query(point)
        matching = [self.cells[int(index)] for index in indices if self.cells[int(index)].geometry.covers(point)]
        if not matching:
            return None, None, RESOLUTION_NO_CELL_MATCH
        # Normally cells are disjoint; for exact-boundary points choose the interior,
        # then the smallest polygon, then lexical token for deterministic behavior.
        matching.sort(key=lambda cell: (not cell.geometry.contains(point), cell.geometry.area, cell.cell_id))
        cell = matching[0]
        value = cell.values.get(precision)
        if value is None:
            return None, cell.cell_id, RESOLUTION_CELL_MISSING_TIER
        return value, cell.cell_id, RESOLUTION_REGIONAL


def threshold_provenance_report(
    records: Iterable[FootprintRecord],
    target_precision: int | None,
    thresholds_path: Path | None,
    thresholds_source: str,
) -> dict[str, Any]:
    """Build report-ready threshold policy plus per-source-feature threshold lineage."""
    source_records = list(records)
    resolution_counts: Counter[str] = Counter()
    applied_thresholds: set[float] = set()
    resolved_cells: set[str] = set()
    per_feature: list[dict[str, Any]] = []
    for record in source_records:
        attributes = record.attributes
        resolution = attributes.get("threshold_resolution_source", RESOLUTION_BASELINE)
        resolution_counts[resolution] += 1
        applied = attributes.get("applied_confidence_threshold")
        if isinstance(applied, (int, float)) and not isinstance(applied, bool):
            applied_thresholds.add(float(applied))
        cell_id = attributes.get("s2_cell_id")
        if cell_id:
            resolved_cells.add(str(cell_id))
        per_feature.append(
            {
                "source_id": record.source_id,
                "confidence": record.confidence,
                "applied_confidence_threshold": applied,
                "target_precision": attributes.get("target_precision"),
                "s2_cell_id": cell_id,
                "threshold_resolution_source": resolution,
            }
        )
    regional = resolution_counts.get(RESOLUTION_REGIONAL, 0)
    if target_precision is None:
        summary = "No precision target requested; every feature used the standard minimum confidence cutoff."
    elif regional:
        summary = (
            f"{regional} of {len(source_records)} source footprints used the S2-cell threshold for the "
            f"estimated {target_precision}% precision tier; the remainder fell back to the standard minimum confidence."
        )
    else:
        summary = (
            f"No S2-cell threshold was available for the estimated {target_precision}% precision tier; "
            "every feature fell back to the standard minimum confidence."
        )
    return {
        "target_precision": target_precision,
        "precision_is_estimate": target_precision is not None,
        "precision_note": (
            "Google publishes these per-cell score thresholds as estimated precision tiers, "
            "not as a guaranteed precision or accuracy statement."
            if target_precision is not None
            else "No regional precision target requested; the standard minimum confidence was used."
        ),
        "thresholds_csv_path": str(thresholds_path) if thresholds_path else None,
        "thresholds_resolution_source": thresholds_source,
        "threshold_resolution_summary": summary,
        "threshold_resolution_counts": dict(sorted(resolution_counts.items())),
        "applied_confidence_thresholds": sorted(applied_thresholds),
        "resolved_s2_cell_ids": sorted(resolved_cells),
        "source_feature_thresholds": per_feature,
    }


class GoogleOpenBuildingsProvider(BaseFootprintProvider):
    """Google Open Buildings v3 polygons with score and row-level provenance."""

    name = "google-open-buildings"
    attribution = OB_ATTRIBUTION
    default_min_confidence = 0.65
    capabilities = SourceCapabilities(
        source_id="google-open-buildings",
        display_name="Google Open Buildings v3",
        license_id="CC-BY-4.0 OR ODbL-1.0",
        default_min_confidence=0.65,
        supports_remote_discovery=True,
        supports_local_cache=True,
        supports_directory_cache=True,
        supports_size_limit=True,
        supports_confidence_scores=True,
        supports_precision_thresholds=True,
        supports_streaming=True,
        accepted_suffixes=(".csv.gz", ".csv"),
        notes=(
            "S2 spatially sharded CSV tiles with per-feature confidence and plus codes.",
            "Publishes per-S2-cell thresholds for estimated 80/85/90% precision.",
        ),
    )

    def __init__(self, max_download_mb: float = 250.0) -> None:
        if max_download_mb <= 0:
            raise ValueError("max_download_mb must be greater than zero")
        self.max_download_mb = float(max_download_mb)

    @staticmethod
    def _local_paths(path: Path) -> list[Path]:
        if path.is_dir():
            paths = sorted((*path.glob("*.csv.gz"), *path.glob("*.csv")))
            if not paths:
                raise FileNotFoundError(f"No Google Open Buildings .csv or .csv.gz files found in {path}")
            return paths
        if not path.is_file():
            raise FileNotFoundError(f"Google Open Buildings cache does not exist: {path}")
        if path.suffix.lower() not in {".csv", ".gz"}:
            raise ValueError("Google Open Buildings input must be a .csv, .csv.gz, or directory of tile files")
        return [path]

    def _layer(self, out_dir: Path) -> AcquisitionLayer:
        return AcquisitionLayer(self.capabilities, cache_dir=out_dir / "source", max_download_mb=self.max_download_mb)

    def acquire_thresholds(self, out_dir: Path, local_path: Path | None = None) -> tuple[Path | None, str]:
        """Use an explicit/local cached thresholds CSV, otherwise download and cache it.

        A failed automatic lookup is non-fatal: callers then apply the normal 0.65
        baseline and record why regional resolution was unavailable.
        """
        if local_path is not None:
            path = local_path.expanduser()
            if not path.is_file():
                raise FileNotFoundError(f"Google score-threshold CSV does not exist: {path}")
            return path, f"local-thresholds-csv:{path}"
        layer = self._layer(out_dir)
        cached = layer.cached(THRESHOLDS_FILENAME)
        if cached is not None:
            return cached, f"cached-thresholds-csv:{cached}"
        try:
            path = layer.fetch(
                RemoteTile(
                    url=OB_SCORE_THRESHOLDS_URL,
                    filename=THRESHOLDS_FILENAME,
                    label="Google score thresholds",
                ),
                integrity="none",
                max_download_mb=THRESHOLDS_MAX_MB,
            )
            return path, f"downloaded-thresholds-csv:{path}"
        except AcquisitionError as error:
            return None, f"thresholds-unavailable:{error}"

    def acquire(
        self,
        lat: float,
        lon: float,
        radius_m: float,
        out_dir: Path,
        local_file: Path | None = None,
    ) -> tuple[list[Path], str]:
        layer = self._layer(out_dir)
        if local_file is not None:
            paths = layer.local_paths(local_file)
            return paths, "local Google Open Buildings v3 cached tile(s): " + ", ".join(p.name for p in paths)

        index_path = layer.cached(TILES_INDEX_FILENAME) or layer.fetch_index(
            RemoteTile(url=OB_TILES_URL, filename=TILES_INDEX_FILENAME, label="Google tiles metadata")
        )
        index = json.loads(index_path.read_text(encoding="utf-8"))

        # A geographic rectangle enclosing the metric search circle is sufficient to
        # select S2 source tiles; every feature is precisely clipped after projection.
        lat_delta = radius_m / 110_574.0
        lon_scale = max(1_000.0, 111_320.0 * abs(math.cos(math.radians(lat))))
        lon_delta = radius_m / lon_scale
        query_extent = box(lon - lon_delta, lat - lat_delta, lon + lon_delta, lat + lat_delta)
        selected = []
        for feature in index.get("features", []):
            try:
                tile_geometry = shape(feature["geometry"])
                properties = feature["properties"]
                if tile_geometry.intersects(query_extent) and properties.get("tile_url"):
                    selected.append(properties)
            except (KeyError, TypeError, ValueError):
                continue
        if not selected:
            raise RuntimeError("Google Open Buildings metadata has no v3 polygon tile intersecting the requested area")

        paths: list[Path] = []
        for tile in selected:
            tile_url = str(tile["tile_url"])
            tile_id = str(tile.get("tile_id", Path(tile_url).name.split("_", 1)[0]))
            size_mb = float(tile.get("size_mb") or 0.0)
            if size_mb > self.max_download_mb:
                raise RuntimeError(
                    f"Google Open Buildings tile {tile_id} is {size_mb:.1f} MB, above the "
                    f"{self.max_download_mb:.1f} MB download limit; download/cache it separately "
                    "and pass --google-open-buildings-file"
                )
            paths.append(
                layer.fetch(
                    RemoteTile(
                        url=tile_url,
                        filename=f"google-open-buildings-{tile_id}_buildings.csv.gz",
                        expected_size_mb=size_mb or None,
                        label=f"tile {tile_id}",
                    ),
                    integrity="gzip",
                )
            )
        source = "Google Open Buildings v3 spatially-sharded polygon tile(s): " + ", ".join(
            str(tile.get("tile_id", "unknown")) for tile in selected
        )
        return paths, source

    def read(
        self,
        paths: Iterable[Path],
        to_utm: Transformer,
        study_circle: Polygon,
        min_confidence: float | None = None,
        *,
        target_precision: int | None = None,
        thresholds_path: Path | None = None,
        thresholds_source: str | None = None,
    ) -> list[FootprintRecord]:
        baseline = self.default_min_confidence if min_confidence is None else float(min_confidence)
        if not 0.0 <= baseline <= 1.0:
            raise ValueError("min_confidence must be between 0 and 1")
        if target_precision not in (None, 80, 85, 90):
            raise ValueError("target_precision must be None, 80, 85, or 90")
        table_error = None
        threshold_table = None
        if target_precision is not None and thresholds_path is not None:
            try:
                threshold_table = GoogleScoreThresholdTable.from_csv(thresholds_path, thresholds_source)
            except (OSError, ValueError) as error:
                # A missing/invalid table must degrade to the baseline cutoff, never abort a build.
                table_error = f"{type(error).__name__}: {error}"
        records: list[FootprintRecord] = []
        for path in paths:
            opener = gzip.open if path.suffix.lower() == ".gz" else open
            with opener(path, "rt", encoding="utf-8-sig", newline="") as stream:
                reader = csv.DictReader(stream)
                required = {"geometry", "confidence"}
                if not reader.fieldnames or not required.issubset(reader.fieldnames):
                    raise ValueError(
                        f"Google Open Buildings CSV needs columns {sorted(required)}; "
                        f"found {reader.fieldnames} in {path}"
                    )
                for row_number, row in enumerate(reader, 2):
                    score = _confidence(row.get("confidence"))
                    if score is None:
                        continue
                    try:
                        geometry = wkt.loads(row.get("geometry", ""))
                    except (ShapelyError, TypeError, ValueError):
                        continue
                    applied_threshold = baseline
                    s2_cell_id = None
                    regional_value = None
                    if target_precision is None:
                        resolution_source = RESOLUTION_BASELINE
                    elif threshold_table is None:
                        resolution_source = RESOLUTION_TABLE_UNAVAILABLE
                    else:
                        regional_value, s2_cell_id, resolution_source = threshold_table.resolve(
                            geometry.centroid, target_precision
                        )
                    if regional_value is not None:
                        applied_threshold = regional_value
                    if score < applied_threshold:
                        continue

                    plus_code = (row.get("full_plus_code") or "").strip()
                    source_id = f"{path.name}:row-{row_number}"
                    attributes: dict[str, Any] = {
                        "tile_file": path.name,
                        "row": row_number,
                        "applied_confidence_threshold": applied_threshold,
                        "target_precision": target_precision,
                        "s2_cell_id": s2_cell_id,
                        "threshold_resolution_source": resolution_source,
                    }
                    if thresholds_source:
                        attributes["threshold_table_source"] = thresholds_source
                    if table_error:
                        attributes["threshold_table_error"] = table_error
                    if plus_code:
                        attributes["full_plus_code"] = plus_code
                    try:
                        area = float(row.get("area_in_meters", ""))
                        if math.isfinite(area) and area >= 0:
                            attributes["area_in_meters"] = area
                    except (TypeError, ValueError):
                        pass
                    records.extend(
                        _project_records(
                            geometry,
                            source_id,
                            self.name,
                            "CC BY 4.0 OR ODbL 1.0",
                            to_utm,
                            study_circle,
                            score,
                            attributes,
                        )
                    )
        return records


def quadkey(lat: float, lon: float, zoom: int = 9) -> str:
    xtile = int((lon + 180.0) / 360.0 * (1 << zoom))
    lat_rad = math.radians(max(-85.05112878, min(85.05112878, lat)))
    ytile = int((1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * (1 << zoom))
    result = []
    for level in range(zoom, 0, -1):
        mask = 1 << (level - 1)
        result.append(str((1 if xtile & mask else 0) + (2 if ytile & mask else 0)))
    return "".join(result)


PROVIDER_CLASSES: dict[str, type[BaseFootprintProvider]] = {
    "microsoft": MicrosoftFootprintProvider,
    "google-open-buildings": GoogleOpenBuildingsProvider,
}


def get_footprint_provider(name: str, max_google_download_mb: float = 250.0) -> BaseFootprintProvider:
    """Create the requested provider; Microsoft remains the backwards-compatible default."""
    if name == "microsoft":
        return MicrosoftFootprintProvider()
    if name == "google-open-buildings":
        return GoogleOpenBuildingsProvider(max_google_download_mb)
    raise ValueError(f"Unsupported footprint provider: {name}")


def provider_capabilities() -> dict[str, dict[str, Any]]:
    """Declared capabilities of every registered source, for reports and tooling.

    Callers and agents read this instead of inferring behaviour from provider
    names, so a new source is discoverable without changing the consumer.
    """
    return {name: cls.capabilities.to_dict() for name, cls in PROVIDER_CLASSES.items()}
