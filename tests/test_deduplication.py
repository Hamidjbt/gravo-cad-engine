"""Regression tests for scale-safe deduplication in ``merge_footprint_records``.

The defect these lock down: duplicate detection used to compare each candidate
only against the first 120 already-retained records (``unique[:120]``). In dense
fabric, or on a large A0 sheet, a duplicate of the 121st-or-later retained
footprint was silently kept. The old window is reproduced here as an oracle so
the tests fail if the bound is ever reintroduced.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from shapely.geometry import box

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from footprint_providers import (  # noqa: E402
    DeduplicationReport,
    FootprintRecord,
    merge_footprint_records,
)

DENSE_COUNT = 400  # comfortably beyond the old 120-record window


def _record(x: float, y: float, side: float, source_id: str, provider: str = "google-open-buildings"):
    return FootprintRecord(box(x, y, x + side, y + side), provider, source_id, 0.9)


def _key(record: FootprintRecord) -> tuple[float, ...]:
    """Canonical geometry identity: rounded bounds, insensitive to ring order."""
    return tuple(round(value, 6) for value in record.geometry.bounds)


def _grid(count: int, *, spacing: float = 50.0, base_side: float = 20.0, shrink: float = 0.01):
    """Deterministic grid; strictly decreasing size fixes the merged sort order."""
    records = []
    for index in range(count):
        side = base_side - index * shrink
        x = (index % 40) * spacing
        y = (index // 40) * spacing
        records.append(_record(x, y, side, f"tile:row-{index}"))
    return records


def _legacy_merge(authoritative, inferred, *, duplicate_overlap: float = 0.88, window: int = 120):
    """The pre-fix algorithm, kept as an oracle for regression comparisons."""
    authoritative_records = [r for r in authoritative if r.geometry.area >= 4.0]
    merged = list(authoritative_records)
    for candidate in inferred:
        if candidate.geometry.area < 5.0:
            continue
        duplicate = any(
            candidate.geometry.intersection(known.geometry).area
            / max(min(candidate.geometry.area, known.geometry.area), 1e-8)
            > 0.62
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
            for previous in unique[:window]
        )
        if not duplicate:
            unique.append(candidate)
    return unique


# --- the reported defect ---------------------------------------------------


def test_duplicate_beyond_the_old_120_record_window_is_detected():
    """The exact failure mode: a duplicate of the 125th retained footprint."""
    records = _grid(125)
    target = records[124]  # smallest -> sorted last -> outside unique[:120]
    duplicate = _record(
        target.geometry.bounds[0],
        target.geometry.bounds[1],
        target.geometry.bounds[2] - target.geometry.bounds[0],
        "tile:duplicate-of-row-124",
    )

    result = merge_footprint_records([], [*records, duplicate])
    # Exactly one copy survives. Which one is settled by the pre-existing
    # (-area, provider, source_id) ordering, so assert on geometry, not identity.
    assert len(result) == 125
    assert sum(1 for record in result if record.geometry.equals(target.geometry)) == 1


def test_legacy_algorithm_really_did_keep_that_duplicate():
    """Guards the test itself: proves the oracle reproduces the old defect."""
    records = _grid(125)
    target = records[124]
    duplicate = _record(
        target.geometry.bounds[0],
        target.geometry.bounds[1],
        target.geometry.bounds[2] - target.geometry.bounds[0],
        "tile:duplicate-of-row-124",
    )
    legacy = _legacy_merge([], [*records, duplicate])
    assert "tile:duplicate-of-row-124" in {record.source_id for record in legacy}
    assert len(legacy) == 126  # the bug: 126 instead of 125


def test_dense_fabric_deduplicates_every_duplicate_at_scale():
    """400 distinct footprints plus a duplicate of each: none may survive."""
    records = _grid(DENSE_COUNT)
    duplicates = []
    for index, original in enumerate(records):
        minx, miny, maxx, _ = original.geometry.bounds
        duplicates.append(_record(minx, miny, maxx - minx, f"tile:dup-{index}"))

    result = merge_footprint_records([], [*records, *duplicates])
    assert len(result) == DENSE_COUNT
    survivors = [_key(record) for record in result]
    for original in records:
        assert _key(original) in survivors
    assert len(set(survivors)) == DENSE_COUNT


def test_smaller_input_than_the_old_window_is_unaffected():
    """Below the former cap both algorithms must agree exactly."""
    records = _grid(40)
    duplicates = []
    for index, original in enumerate(records):
        minx, miny, maxx, _ = original.geometry.bounds
        duplicates.append(_record(minx, miny, maxx - minx, f"tile:dup-{index}"))
    payload = [*records, *duplicates]
    assert [r.source_id for r in merge_footprint_records([], payload)] == [
        r.source_id for r in _legacy_merge([], payload)
    ]


def test_distinct_dense_footprints_are_all_retained():
    """Deduplication must not become over-eager under spatial indexing."""
    records = _grid(DENSE_COUNT)
    result = merge_footprint_records([], records)
    assert len(result) == DENSE_COUNT


def test_duplicate_of_the_first_record_is_still_detected():
    """The index must not lose early records either."""
    records = _grid(DENSE_COUNT)
    first = records[0]
    minx, miny, maxx, _ = first.geometry.bounds
    duplicate = _record(minx, miny, maxx - minx, "tile:dup-first")
    result = merge_footprint_records([], [*records, duplicate])
    assert len(result) == DENSE_COUNT
    assert sum(1 for record in result if record.geometry.equals(first.geometry)) == 1


# --- provenance and counters ----------------------------------------------


def test_deduplication_report_counts_every_disposition():
    records = _grid(200)
    duplicates = []
    for original in records[:50]:
        minx, miny, maxx, _ = original.geometry.bounds
        duplicates.append(_record(minx, miny, maxx - minx, f"tile:dup-{original.source_id}"))
    tiny = _record(10_000.0, 10_000.0, 1.0, "tile:tiny")  # below minimum_area_m2
    osm = FootprintRecord(box(-100, -100, -80, -80), "osm", "osm:1", None)
    overlapping_inferred = FootprintRecord(box(-100, -100, -80, -80), "google-open-buildings", "tile:osm-dup", 0.9)

    report = DeduplicationReport()
    result = merge_footprint_records([osm], [*records, *duplicates, tiny, overlapping_inferred], report=report)

    assert report.authoritative_in == 1
    assert report.inferred_in == 200 + 50 + 1 + 1
    assert report.dropped_below_minimum_area == 1
    assert report.dropped_as_authoritative_duplicate == 1
    assert report.dropped_as_duplicate == 50
    assert report.output_count == len(result) == 201
    assert report.index_filtered is True
    assert report.bounded_window is None
    assert report.candidates_skipped_by_index > 0


def test_report_is_serialisable_for_the_build_json():
    report = DeduplicationReport()
    merge_footprint_records([], _grid(10), report=report)
    payload = report.to_dict()
    assert set(payload) >= {
        "authoritative_in",
        "inferred_in",
        "dropped_as_duplicate",
        "output_count",
        "candidates_compared",
        "candidates_skipped_by_index",
        "bounded_window",
    }
    assert payload["output_count"] == 10


def test_build_summary_embeds_deduplication_provenance():
    import importlib.util

    spec = importlib.util.spec_from_file_location("dedup_build", ROOT / "tools" / "cad-fond-de-plan-build.py")
    assert spec is not None and spec.loader is not None
    build = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(build)

    records = _grid(130)
    report = DeduplicationReport()
    unique = merge_footprint_records([], records, report=report)
    provider = __import__("footprint_providers").MicrosoftFootprintProvider()
    summary = build.build_footprint_summary(
        provider,
        "fixture",
        [ROOT / "tests" / "fixtures" / "google_open_buildings_sample.csv"],
        None,
        records,
        unique,
        unique,
        None,
        None,
        "not-requested",
        report,
    )
    assert summary["deduplication"]["output_count"] == 130
    assert summary["deduplication"]["index_filtered"] is True


# --- performance guard ----------------------------------------------------


def test_index_skips_most_comparisons_in_dense_fabric():
    """Guards against a return to exhaustive scanning.

    With a spatial index, the overwhelming majority of retained records are
    out of range for any given candidate. If the bound is ever reintroduced or
    the index is bypassed, ``candidates_skipped_by_index`` collapses toward zero.
    """
    records = _grid(DENSE_COUNT)
    report = DeduplicationReport()
    merge_footprint_records([], records, report=report)
    assert report.index_filtered is True
    assert report.candidates_compared > 0  # the index is actually consulted
    assert report.candidates_skipped_by_index > 10 * report.candidates_compared


def test_large_sheet_scale_completes_without_quadratic_blowup():
    """2000 footprints must merge in well under a second (was ~0.5 s/5k quadratic)."""
    import time

    records = _grid(2000, spacing=22.0, base_side=12.0, shrink=0.001)
    start = time.perf_counter()
    result = merge_footprint_records([], records)
    elapsed = time.perf_counter() - start
    assert len(result) == 2000
    assert elapsed < 2.0, f"dedup took {elapsed:.2f}s for 2000 footprints"


# --- explicit, opt-in bounds ----------------------------------------------


def test_explicit_window_is_honoured_and_reported():
    """A caller may still ask for a bounded window; it must be visible, not silent."""
    records = _grid(125)
    report = DeduplicationReport()
    merge_footprint_records([], records, dedup_window=120, report=report)
    assert report.bounded_window == 120


def test_opt_in_window_reproduces_the_old_limit_when_requested():
    records = _grid(125)
    target = records[124]
    minx, miny, maxx, _ = target.geometry.bounds
    duplicate = _record(minx, miny, maxx - minx, "tile:dup-row-124")
    bounded = merge_footprint_records([], [*records, duplicate], dedup_window=120)
    unbounded = merge_footprint_records([], [*records, duplicate])
    assert len(bounded) == 126  # the explicit cap still loses it...
    assert len(unbounded) == 125  # ...while the default is correct


def test_zero_or_negative_window_means_unbounded():
    records = _grid(130)
    for window in (0, -5):
        report = DeduplicationReport()
        assert len(merge_footprint_records([], records, dedup_window=window, report=report)) == 130
        assert report.bounded_window is None


# --- parity with the reference implementation ----------------------------


def _reference_merge(inferred, *, window: int = 10**9, duplicate_overlap: float = 0.88):
    """The pre-fix algorithm with an unbounded window: correct reference semantics.

    ``window`` defaults to effectively infinite here, so this expresses what the
    old code *intended* rather than what its 120-cap actually did.
    """
    merged = list(inferred)
    merged.sort(key=lambda record: (-record.geometry.area, record.provider, record.source_id))
    unique: list[FootprintRecord] = []
    for candidate in merged:
        duplicate = any(
            candidate.geometry.intersection(previous.geometry).area
            / max(min(candidate.geometry.area, previous.geometry.area), 1e-8)
            > duplicate_overlap
            for previous in unique[:window]
        )
        if not duplicate:
            unique.append(candidate)
    return [(record.source_id, round(record.geometry.area, 6)) for record in unique]


@pytest.mark.parametrize("count", [40, 200, 900])
def test_indexed_merge_matches_reference_semantics(count):
    """Same results as the exhaustive algorithm, in bounded time."""
    records = _grid(count)
    duplicates = []
    for original in records[::7]:
        minx, miny, maxx, _ = original.geometry.bounds
        duplicates.append(_record(minx, miny, maxx - minx, f"tile:dup-{original.source_id}"))
    payload = [*records, *duplicates]
    assert [
        (record.source_id, round(record.geometry.area, 6)) for record in merge_footprint_records([], payload)
    ] == _reference_merge(payload)


def test_oversized_footprint_among_small_ones_still_matches_reference():
    """A warehouse larger than the bucket grid lands on the always-scanned list.

    Under v1.8.0 the default policy is ``nested``, so the exhaustive reference
    (which encodes absorption) is a parity oracle for the ``absorb`` policy only.
    Both are asserted: the index must agree with the reference under absorb, and
    must retain everything but the exact duplicate under the default.
    """
    from reconciliation import ContainmentPolicy, ReconciliationPolicy

    small = [_record((i % 50) * 20.0, (i // 50) * 20.0, 8.0, f"tile:s{i}") for i in range(300)]
    warehouse = _record(0, 0, 900.0, "tile:warehouse")
    duplicate = FootprintRecord(box(0, 0, 900, 400), "google-open-buildings", "tile:warehouse-dup", 0.9)
    payload = [*small, warehouse, duplicate]

    absorbing = merge_footprint_records(
        [], payload, policy=ReconciliationPolicy(containment_policy=ContainmentPolicy.ABSORB)
    )
    assert [(record.source_id, round(record.geometry.area, 6)) for record in absorbing] == _reference_merge(payload)

    nested = merge_footprint_records([], payload)
    # `warehouse` is 900x900; `duplicate` is 900x400 inside it. The old scale-blind
    # metric scored that containment as 1.0 and absorbed it despite only 44% outline
    # coincidence (IoU), so v1.7.0 collapsed 302 -> 31. IoU now calls it containment,
    # and nesting retains every footprint including the enclosed rectangle.
    assert len(nested) == len(payload)
    assert sum(1 for record in nested if record.geometry.bounds == warehouse.geometry.bounds) == 1
    assert sum(1 for record in nested if record.geometry.bounds == duplicate.geometry.bounds) == 1


# --- preserved behaviour --------------------------------------------------


def test_osm_still_wins_over_overlapping_inferred_records():
    osm = FootprintRecord(box(0, 0, 10, 10), "osm", "osm:123", None, "ODbL-1.0")
    duplicate = FootprintRecord(box(0.1, 0.1, 10.1, 10.1), "google-open-buildings", "tile:row-2", 0.97)
    distinct = FootprintRecord(box(30, 0, 40, 10), "google-open-buildings", "tile:row-3", 0.81)
    result = merge_footprint_records([osm], [duplicate, distinct])
    assert {record.source_id for record in result} == {"osm:123", "tile:row-3"}
    kept = next(record for record in result if record.source_id == "tile:row-3")
    assert kept.confidence == 0.81  # confidence stays attached to its footprint


def test_tiny_footprints_are_dropped_before_deduplication():
    small = _record(0, 0, 1.0, "tile:tiny")  # 1 m2, below the 5 m2 floor
    kept = _record(50, 50, 10.0, "tile:ok")
    result = merge_footprint_records([], [small, kept], minimum_area_m2=5.0)
    assert [record.source_id for record in result] == ["tile:ok"]


def test_output_is_sorted_by_descending_area():
    records = [_record(0, 0, 5.0, "a"), _record(100, 0, 20.0, "b"), _record(200, 0, 10.0, "c")]
    result = merge_footprint_records([], records)
    assert [record.source_id for record in result] == ["b", "c", "a"]


def test_empty_inputs_return_empty_output():
    assert merge_footprint_records([], []) == []


@pytest.mark.parametrize("count", [1, 2, 119, 120, 121])
def test_window_boundary_counts_are_all_correct(count):
    records = _grid(count)
    target = records[-1]
    minx, miny, maxx, _ = target.geometry.bounds
    duplicate = _record(minx, miny, maxx - minx, "tile:dup-last")
    result = merge_footprint_records([], [*records, duplicate])
    assert len(result) == count
