"""Offline regression coverage for footprint provider parsing and lineage."""

from __future__ import annotations

import gzip
import importlib.util
import json
import sys
from pathlib import Path

import pytest
from pyproj import Transformer
from shapely import wkt
from shapely.geometry import Point, Polygon, box, mapping

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
sys.path.insert(0, str(TOOLS))

from footprint_providers import (  # noqa: E402
    FootprintRecord,
    GoogleOpenBuildingsProvider,
    GoogleScoreThresholdTable,
    MicrosoftFootprintProvider,
    S2ConfidenceThreshold,
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
        [lon, lat],
        [lon + 0.0001, lat],
        [lon + 0.0001, lat + 0.0001],
        [lon, lat + 0.0001],
        [lon, lat],
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


# --- v1.4.4: region-aware S2-cell confidence thresholds ---------------------

THRESHOLDS_FIXTURE = ROOT / "tests" / "fixtures" / "google_open_buildings_score_thresholds.csv"
WIDE_RADIUS_M = 500.0  # includes all three sample tiles rows for threshold filtering


def _records_at(paths, *, target_precision=None, thresholds_path=None, source="test-thresholds", radius=WIDE_RADIUS_M):
    provider = GoogleOpenBuildingsProvider()
    to_utm = Transformer.from_crs(4326, 32630, always_xy=True)
    x, y = to_utm.transform(-0.641, 35.700)
    return provider.read(
        paths,
        to_utm,
        Point(x, y).buffer(radius),
        None,
        target_precision=target_precision,
        thresholds_path=thresholds_path,
        thresholds_source=source,
    )


def test_precision_threshold_uses_exact_s2_cell_value():
    """Every 90%-tier row inside the Oran fixture cell resolves that cell's own value."""
    records = _records_at([FIXTURE], target_precision=90, thresholds_path=THRESHOLDS_FIXTURE)
    by_row = {record.source_id.rsplit("row-", 1)[-1]: record for record in records}
    assert set(by_row) == {"2", "4"}  # the 0.60 row (row 3) is filtered by the 0.90 regional cutoff
    record = by_row["2"]
    assert record.confidence == 0.93
    assert record.attributes["applied_confidence_threshold"] == 0.90
    assert record.attributes["target_precision"] == 90
    assert record.attributes["s2_cell_id"] == "oran-cell-test"
    assert record.attributes["threshold_resolution_source"] == "regional-s2-cell-threshold"
    assert record.attributes["threshold_table_source"] == "test-thresholds"


def test_precision_tier_changes_the_applied_cutoff():
    """The 80% tier keeps the 0.60-scored row; the 90% tier drops it."""
    loosened = _records_at([FIXTURE], target_precision=80, thresholds_path=THRESHOLDS_FIXTURE)
    tightened = _records_at([FIXTURE], target_precision=90, thresholds_path=THRESHOLDS_FIXTURE)
    assert [record.confidence for record in loosened] == [0.93, 0.60, 0.99]
    assert [record.confidence for record in tightened] == [0.93, 0.99]
    assert {record.attributes["applied_confidence_threshold"] for record in loosened if record.confidence == 0.60} == {
        0.55
    }
    assert 0.90 in {record.attributes["applied_confidence_threshold"] for record in tightened}


def test_missing_cell_value_falls_back_to_baseline_without_crashing():
    """unscored-cell-test publishes no 90% value, so the baseline 0.65 must apply."""
    records = _records_at([FIXTURE], target_precision=90, thresholds_path=THRESHOLDS_FIXTURE)
    fallback_records = [r for r in records if r.attributes["s2_cell_id"] != "oran-cell-test"]
    assert len(fallback_records) == 1
    record = fallback_records[0]
    assert record.confidence == 0.99
    assert record.attributes["applied_confidence_threshold"] == 0.65
    assert record.attributes["target_precision"] == 90
    assert record.attributes["s2_cell_id"] == "unscored-cell-test"
    assert record.attributes["threshold_resolution_source"] == "fallback-cell-threshold-missing"


def test_point_outside_every_cell_falls_back_to_baseline():
    table = GoogleScoreThresholdTable.from_csv(THRESHOLDS_FIXTURE)
    value, cell_id, resolution = table.resolve(Point(2.0, 48.0), 85)
    assert value is None
    assert cell_id is None
    assert resolution == "fallback-no-cell-match"


def test_shared_cell_boundary_resolution_is_deterministic():
    """A point on a shared edge picks one cell reproducibly instead of crashing."""
    left = "POLYGON ((0 0, 1 0, 1 1, 0 1, 0 0))"
    right = "POLYGON ((1 0, 2 0, 2 1, 1 1, 1 0))"
    cells = [
        S2ConfidenceThreshold("cell-left", wkt.loads(left), {80: 0.6}),
        S2ConfidenceThreshold("cell-right", wkt.loads(right), {80: 0.9}),
    ]
    table = GoogleScoreThresholdTable(cells, "inline")
    first = table.resolve(Point(1.0, 0.5), 80)
    second = table.resolve(Point(1.0, 0.5), 80)
    assert first == second
    assert first[1] in {"cell-left", "cell-right"}
    assert first[0] in {0.6, 0.9}


def test_invalid_precision_is_rejected():
    provider = GoogleOpenBuildingsProvider()
    to_utm, study_circle = _projection_and_study_circle()
    for bad_precision in (70, 95, 0, -90):
        with pytest.raises(ValueError, match="target_precision"):
            provider.read(
                [FIXTURE],
                to_utm,
                study_circle,
                target_precision=bad_precision,
                thresholds_path=THRESHOLDS_FIXTURE,
            )


def test_unreadable_threshold_table_degrades_to_baseline(tmp_path):
    broken = tmp_path / "thresholds.csv"
    broken.write_text("s2_token,geometry\noran,not-a-polygon\n", encoding="utf-8")
    records = _records_at([FIXTURE], target_precision=85, thresholds_path=broken)
    assert records
    for record in records:
        assert record.attributes["threshold_resolution_source"] == "fallback-threshold-table-unavailable"
        assert record.attributes["applied_confidence_threshold"] == 0.65
        assert "threshold_table_error" in record.attributes


def test_missing_threshold_file_at_acquire_is_reported(tmp_path):
    provider = GoogleOpenBuildingsProvider()
    with pytest.raises(FileNotFoundError, match="score-threshold CSV does not exist"):
        provider.acquire_thresholds(tmp_path, tmp_path / "absent.csv")


def test_local_threshold_csv_is_used_without_network(tmp_path):
    provider = GoogleOpenBuildingsProvider()
    path, source = provider.acquire_thresholds(tmp_path, THRESHOLDS_FIXTURE)
    assert path == THRESHOLDS_FIXTURE
    assert source.startswith("local-thresholds-csv:")


def test_cached_threshold_csv_is_reused_without_network(tmp_path):
    provider = GoogleOpenBuildingsProvider()
    cached = tmp_path / "source" / "google-open-buildings-score-thresholds.csv"
    cached.parent.mkdir(parents=True)
    cached.write_text(THRESHOLDS_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
    path, source = provider.acquire_thresholds(tmp_path)
    assert path == cached
    assert source.startswith("cached-thresholds-csv:")


def test_default_precision_none_preserves_baseline_provenance():
    records = _records_at([FIXTURE])
    assert [record.confidence for record in records] == [0.93, 0.99]
    for record in records:
        assert record.attributes["applied_confidence_threshold"] == 0.65
        assert record.attributes["target_precision"] is None
        assert record.attributes["s2_cell_id"] is None
        assert record.attributes["threshold_resolution_source"] == "baseline-min-confidence"


def test_report_provenance_records_threshold_policy_and_estimates():
    records = _records_at([FIXTURE], target_precision=90, thresholds_path=THRESHOLDS_FIXTURE)
    provider = GoogleOpenBuildingsProvider()
    summary = build.build_footprint_summary(
        provider,
        "local Google Open Buildings fixture",
        [FIXTURE],
        None,
        records,
        records,
        records,
        90,
        THRESHOLDS_FIXTURE,
        f"local-thresholds-csv:{THRESHOLDS_FIXTURE}",
    )

    assert summary["target_precision"] == 90
    assert summary["precision_is_estimate"] is True
    assert "estimate" in summary["precision_note"]
    assert summary["thresholds_csv_path"] == str(THRESHOLDS_FIXTURE)
    assert summary["thresholds_resolution_source"].startswith("local-thresholds-csv:")
    assert summary["applied_confidence_thresholds"] == [0.65, 0.9]
    assert summary["resolved_s2_cell_ids"] == ["oran-cell-test", "unscored-cell-test"]
    assert summary["threshold_resolution_counts"] == {
        "fallback-cell-threshold-missing": 1,
        "regional-s2-cell-threshold": 1,
    }
    assert "estimated 90% precision tier" in summary["threshold_resolution_summary"]
    assert len(summary["source_feature_thresholds"]) == len(records)
    entry = next(item for item in summary["source_feature_thresholds"] if item["s2_cell_id"] == "oran-cell-test")
    assert entry["applied_confidence_threshold"] == 0.90
    assert entry["target_precision"] == 90
    assert entry["threshold_resolution_source"] == "regional-s2-cell-threshold"
    assert len(summary["output_provenance"]) == len(records)


def test_report_provenance_without_precision_target_states_baseline():
    records = _records_at([FIXTURE])
    provider = GoogleOpenBuildingsProvider()
    summary = build.build_footprint_summary(
        provider, "fixture", [FIXTURE], None, records, records, records, None, None, "not-requested"
    )
    assert summary["target_precision"] is None
    assert summary["precision_is_estimate"] is False
    assert summary["threshold_resolution_counts"] == {"baseline-min-confidence": 2}
    assert summary["precision_note"].startswith("No regional precision target requested")
    assert summary["thresholds_csv_path"] is None


def test_cli_exposes_precision_and_thresholds_flags_with_baseline_default():
    parser = build.build_parser()
    defaults = parser.parse_args([])
    assert defaults.google_target_precision is None  # baseline 0.65 cutoff preserved
    assert defaults.google_thresholds_path is None
    assert defaults.google_min_confidence == 0.65

    chosen = parser.parse_args(
        [
            "--footprint-provider",
            "google-open-buildings",
            "--google-target-precision",
            "85",
            "--google-thresholds-path",
            "thresholds.csv",
        ]
    )
    assert chosen.google_target_precision == 85
    assert chosen.google_thresholds_path == "thresholds.csv"

    with pytest.raises(SystemExit):
        parser.parse_args(["--google-target-precision", "75"])
