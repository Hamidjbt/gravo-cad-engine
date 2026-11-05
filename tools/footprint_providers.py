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
import os
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests
from pyproj import Transformer
from shapely import make_valid, wkt
from shapely import transform as shapely_transform
from shapely.errors import ShapelyError
from shapely.geometry import GeometryCollection, MultiPolygon, Polygon, box, shape

MS_INDEX = "https://bfppub.blob.core.windows.net/$web/2026-07-24/dataset-links.csv"
OB_TILES_URL = "https://openbuildings-public-dot-gweb-research.uw.r.appspot.com/public/tiles.geojson"
OB_ATTRIBUTION = "Google Open Buildings v3 (CC BY 4.0 or ODbL 1.0)"
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

    def acquire(
        self,
        lat: float,
        lon: float,
        radius_m: float,
        out_dir: Path,
        local_file: Path | None = None,
    ) -> tuple[list[Path], str]:
        del radius_m
        if local_file:
            return [local_file], f"local Microsoft footprint tile: {local_file}"
        src_dir = out_dir / "source"
        src_dir.mkdir(parents=True, exist_ok=True)
        index_path = src_dir / "microsoft-dataset-links.csv"
        if not index_path.exists():
            response = requests.get(MS_INDEX, timeout=180, headers={"User-Agent": "cad-fond-de-plan/1.0"})
            response.raise_for_status()
            index_path.write_bytes(response.content)
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
        tile_path = src_dir / f"microsoft-{row['Location']}-{row['QuadKey']}.csv.gz"
        if not tile_path.exists():
            response = requests.get(row["Url"], timeout=240, headers={"User-Agent": "cad-fond-de-plan/1.0"})
            response.raise_for_status()
            tile_path.write_bytes(response.content)
        return [tile_path], (
            f"Microsoft Global ML Building Footprints, {row['Location']} quadkey {row['QuadKey']} ({row['Url']})"
        )

    def read(
        self,
        paths: Iterable[Path],
        to_utm: Transformer,
        study_circle: Polygon,
        min_confidence: float | None = None,
    ) -> list[FootprintRecord]:
        del min_confidence
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


class GoogleOpenBuildingsProvider(BaseFootprintProvider):
    """Google Open Buildings v3 polygons with score and row-level provenance."""

    name = "google-open-buildings"
    attribution = OB_ATTRIBUTION
    default_min_confidence = 0.65

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

    def acquire(
        self,
        lat: float,
        lon: float,
        radius_m: float,
        out_dir: Path,
        local_file: Path | None = None,
    ) -> tuple[list[Path], str]:
        if local_file is not None:
            paths = self._local_paths(local_file)
            return paths, "local Google Open Buildings v3 cached tile(s): " + ", ".join(p.name for p in paths)

        source_dir = out_dir / "source"
        source_dir.mkdir(parents=True, exist_ok=True)
        index_path = source_dir / "google-open-buildings-tiles.geojson"
        if not index_path.exists():
            response = requests.get(OB_TILES_URL, timeout=120, headers={"User-Agent": "cad-fond-de-plan/1.0"})
            response.raise_for_status()
            index_path.write_bytes(response.content)
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
            path = source_dir / f"google-open-buildings-{tile_id}_buildings.csv.gz"
            if not path.exists():
                size_mb = float(tile.get("size_mb") or 0.0)
                if size_mb > self.max_download_mb:
                    raise RuntimeError(
                        f"Google Open Buildings tile {tile_id} is {size_mb:.1f} MB, above the "
                        f"{self.max_download_mb:.1f} MB download limit; download/cache it separately "
                        "and pass --google-open-buildings-file"
                    )
                self._download_tile(tile_url, path)
            paths.append(path)
        source = "Google Open Buildings v3 spatially-sharded polygon tile(s): " + ", ".join(
            str(tile.get("tile_id", "unknown")) for tile in selected
        )
        return paths, source

    def _download_tile(self, url: str, destination: Path) -> None:
        """Stream a tile to a temporary file and atomically cache the completed gzip."""
        max_bytes = int(self.max_download_mb * 1024 * 1024)
        response = requests.get(
            url,
            timeout=240,
            headers={"User-Agent": "cad-fond-de-plan/1.0"},
            stream=True,
        )
        response.raise_for_status()
        header_size = response.headers.get("Content-Length")
        if header_size and int(header_size) > max_bytes:
            response.close()
            raise RuntimeError(
                f"Google Open Buildings tile exceeds the {self.max_download_mb:.1f} MB download limit; "
                "provide a pre-downloaded cache file instead"
            )
        fd, temp_name = tempfile.mkstemp(prefix=destination.name + ".", suffix=".part", dir=destination.parent)
        os.close(fd)
        temp_path = Path(temp_name)
        received = 0
        try:
            with response, temp_path.open("wb") as stream:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if not chunk:
                        continue
                    received += len(chunk)
                    if received > max_bytes:
                        raise RuntimeError(
                            f"Google Open Buildings tile exceeds the {self.max_download_mb:.1f} MB download limit; "
                            "provide a pre-downloaded cache file instead"
                        )
                    stream.write(chunk)
            temp_path.replace(destination)
        finally:
            temp_path.unlink(missing_ok=True)

    def read(
        self,
        paths: Iterable[Path],
        to_utm: Transformer,
        study_circle: Polygon,
        min_confidence: float | None = None,
    ) -> list[FootprintRecord]:
        threshold = self.default_min_confidence if min_confidence is None else float(min_confidence)
        if not 0.0 <= threshold <= 1.0:
            raise ValueError("min_confidence must be between 0 and 1")
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
                    if score is None or score < threshold:
                        continue
                    try:
                        geometry = wkt.loads(row.get("geometry", ""))
                    except (ShapelyError, TypeError, ValueError):
                        continue
                    plus_code = (row.get("full_plus_code") or "").strip()
                    source_id = f"{path.name}:row-{row_number}"
                    attributes: dict[str, Any] = {"tile_file": path.name, "row": row_number}
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


def get_footprint_provider(name: str, max_google_download_mb: float = 250.0) -> BaseFootprintProvider:
    """Create the requested provider; Microsoft remains the backwards-compatible default."""
    if name == "microsoft":
        return MicrosoftFootprintProvider()
    if name == "google-open-buildings":
        return GoogleOpenBuildingsProvider(max_google_download_mb)
    raise ValueError(f"Unsupported footprint provider: {name}")
