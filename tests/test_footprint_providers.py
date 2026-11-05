"""Offline regression coverage for footprint provider parsing and lineage."""
from __future__ import annotations

import gzip
import importlib.util
import json
import sys
from pathlib import Path

import pytest
from pyproj import Transformer
from shapely.geometry import Point, Polygon, box, mapping

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
sys.path.insert(0, str(TOOLS))

from footprint_providers import (  # noqa: E402
    FootprintRecord,
    GoogleOpenBuildingsProvider,
    MicrosoftFootprintProvider,
    get_footprint_provider,
    merge_footprint_records,
)

FIXTURE = ROOT / "tests" / "fixtures" / "google_open_buildings_sample.csv"
BUILD_SPEC = importlib.util.spec_from_file_location("footprint_build", TOOLS / "cad-fond-de-plan-build.py")
assert BUILD_SPEC is not None and BUILD_SPEC.loader is not None
build = importlib.util.module_from_spec(BUILD_SPEC)
BUILD_SPEC.loader.exec_module(build)


def _projection_and_study_circle():
    to_utm = Transformer.from_crs(4326, 32630, always_xy=True)
    x, y = to_utm.transform(-0.641, 35.700)
    return to_utm, Point(x, y).buffer(100)


def test_google_open_buildings_reads_cached_csv_and_preserves_score_and_lineage():
    provider = GoogleOpenBuildingsProvider()
    to_utm, study_circle = _projection_and_study_circle()
    paths, source = provider.acquire(35.7, -0.641, 100, ROOT, FIXTURE)
    records = provider.read(paths, to_utm, study_circle)

    assert paths == [FIXTURE]
    assert "local Google Open Buildings" in source
    assert len(records) == 1  # below-threshold and out-of-radius rows are excluded
    record = records[0]
    assert record.provider == "google-open-buildings"
    assert record.source_id == f"{FIXTURE.name}:row-2"
    assert record.confidence == 0.93
    assert record.license == "CC BY 4.0 OR ODbL 1.0"
    assert record.attributes["full_plus_code"] == "8C7X9XXX+XX"
    assert record.geometry.is_valid
    assert record.geometry.area > 0
    assert record.provenance(0)["output_index"] == 0


def test_google_records_enter_normalization_and_topology_with_aligned_provenance():
    provider = GoogleOpenBuildingsProvider()
    to_utm, study_circle = _projection_and_study_circle()
    records = provider.read([FIXTURE], to_utm, study_circle)

    topology, lineage, fallbacks, removed = build.regularize_footprint_records(records, Polygon(), study_circle)
    assert len(topology.footprints) == len(lineage) == 1
    assert topology.footprints[0].is_valid
    assert lineage[0].source_id == records[0].source_id
    assert lineage[0].confidence == 0.93
    assert fallbacks >= 0 and removed == 0


def test_google_provider_skips_malformed_wkt_rows():
    provider = GoogleOpenBuildingsProvider()
    malformed = ROOT / "tests" / "fixtures" / "google_open_buildings_malformed.csv"
    records = provider.read([malformed], *_projection_and_study_circle())
    assert records == []


def test_google_open_buildings_honors_custom_confidence_threshold():
    provider = GoogleOpenBuildingsProvider()
    to_utm, study_circle = _projection_and_study_circle()
    records = provider.read([FIXTURE], to_utm, study_circle, min_confidence=0.5)
    assert [record.confidence for record in records] == [0.93, 0.60]


def test_google_provider_accepts_cached_directory_without_network(tmp_path):
    cached = tmp_path / "tile-cache"
    cached.mkdir()
    tile = cached / "tile.csv"
    tile.write_text(FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
    paths, source = GoogleOpenBuildingsProvider().acquire(35.7, -0.641, 100, tmp_path, cached)
    assert paths == [tile]
    assert tile.name in source


def test_google_provider_reports_bad_input_and_threshold(tmp_path):
    provider = GoogleOpenBuildingsProvider()
    with pytest.raises(ValueError, match="between 0 and 1"):
        provider.read([FIXTURE], *_projection_and_study_circle(), min_confidence=1.1)
    with pytest.raises(FileNotFoundError, match="does not exist"):
        provider.acquire(35.7, -0.641, 100, tmp_path, tmp_path / "missing.csv")


def test_microsoft_remains_factory_default_and_reads_cached_geojsonl(tmp_path):
    provider = get_footprint_provider("microsoft")
    assert isinstance(provider, MicrosoftFootprintProvider)
    to_utm, study_circle = _projection_and_study_circle()
    lon, lat = -0.641, 35.7
    coordinates = [
        [lon, lat], [lon + 0.0001, lat], [lon + 0.0001, lat + 0.0001],
        [lon, lat + 0.0001], [lon, lat],
    ]
    feature = {
        "type": "Feature",
        "id": "ms-feature-17",
        "properties": {"confidence": 0.88},
        "geometry": mapping(Polygon(coordinates)),
    }
    cache = tmp_path / "microsoft-tile.csv.gz"
    with gzip.open(cache, "wt", encoding="utf-8") as stream:
        stream.write(json.dumps(feature) + "\n")
    paths, source = provider.acquire(lat, lon, 100, tmp_path, cache)
    records = provider.read(paths, to_utm, study_circle)
    assert paths == [cache]
    assert "local Microsoft" in source
    assert len(records) == 1
    assert records[0].source_id == "ms-feature-17"
    assert records[0].confidence == 0.88


def test_merge_prefers_osm_and_keeps_inferred_confidence_for_nonduplicates():
    shared = box(0, 0, 10, 10)
    osm = FootprintRecord(shared, "osm", "osm:123", license="ODbL-1.0")
    duplicate_google = FootprintRecord(box(0.1, 0.1, 10.1, 10.1), "google-open-buildings", "tile:row-2", 0.97)
    distinct_google = FootprintRecord(box(30, 0, 40, 10), "google-open-buildings", "tile:row-3", 0.81)

    records = merge_footprint_records([osm], [duplicate_google, distinct_google])
    by_id = {record.source_id: record for record in records}
    assert set(by_id) == {"osm:123", "tile:row-3"}
    assert by_id["osm:123"].provider == "osm"
    assert by_id["tile:row-3"].provider == "google-open-buildings"
    assert by_id["tile:row-3"].confidence == 0.81
