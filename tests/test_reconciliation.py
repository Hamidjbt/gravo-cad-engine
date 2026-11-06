"""Tests for the explicit containment policy (v1.8.0).

These lock down three things the old scale-blind metric could not express:

* containment and duplication are *different* questions;
* the answer to "what happens when one footprint encloses another" is a declared
  policy, not an implicit absorption;
* an inferred footprint that envelops authoritative records is retained and
  tagged instead of discarded wholesale.
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
from reconciliation import (  # noqa: E402
    CLASS_CONTAINMENT,
    CLASS_DISTINCT,
    CLASS_DUPLICATE,
    DIRECTION_CONTAINED_BY,
    DIRECTION_CONTAINS,
    ContainmentPolicy,
    OverlapMetrics,
    ReconciliationLog,
    ReconciliationPolicy,
    classify,
    decide,
)

POLICY = ReconciliationPolicy()


def _rec(geometry, source_id: str, provider: str = "google-open-buildings", confidence=0.9):
    return FootprintRecord(geometry, provider, source_id, confidence)


# --- the metric that motivated the milestone ---------------------------------


def test_metrics_separate_containment_from_duplication():
    """The old score was ~1.0 for both cases; IoU separates them by orders of magnitude."""
    warehouse = box(0, 0, 900, 400)
    shop = box(100, 100, 108, 108)
    duplicate = box(0.1, 0.1, 900.1, 400.1)

    containment = OverlapMetrics.between(shop, warehouse)
    duplication = OverlapMetrics.between(warehouse, duplicate)

    # The historical metric cannot tell them apart...
    assert containment.containment == pytest.approx(1.0)
    assert duplication.containment > 0.99
    # ...but IoU does, decisively.
    assert containment.iou < 0.01
    assert duplication.iou > 0.99
    assert duplication.iou > 1000 * containment.iou


def test_classification_distinguishes_the_two_phenomena():
    warehouse = box(0, 0, 900, 400)
    shop = box(100, 100, 108, 108)
    duplicate = box(0.1, 0.1, 900.1, 400.1)

    assert classify(shop, warehouse, POLICY).kind == CLASS_CONTAINMENT
    assert classify(warehouse, duplicate, POLICY).kind == CLASS_DUPLICATE
    assert classify(box(0, 0, 10, 10), box(500, 500, 510, 510), POLICY).kind == CLASS_DISTINCT


def test_direction_reports_which_footprint_encloses_which():
    small, large = box(0, 0, 10, 10), box(0, 0, 900, 400)
    assert classify(small, large, POLICY).direction == DIRECTION_CONTAINED_BY
    assert classify(large, small, POLICY).direction == DIRECTION_CONTAINS


def test_metrics_are_serialisable():
    payload = OverlapMetrics.between(box(0, 0, 10, 10), box(0, 0, 20, 20)).to_dict()
    assert set(payload) == {
        "intersection_m2",
        "containment",
        "iou",
        "area_ratio",
        "larger_area_m2",
        "smaller_area_m2",
    }


# --- policy vocabulary ------------------------------------------------------


def test_policy_coercion_accepts_names_and_rejects_nonsense():
    assert ContainmentPolicy.coerce("nested") is ContainmentPolicy.NESTED
    assert ContainmentPolicy.coerce(" ABSORB ") is ContainmentPolicy.ABSORB
    assert ContainmentPolicy.coerce(ContainmentPolicy.NESTED) is ContainmentPolicy.NESTED
    with pytest.raises(ValueError, match="Unknown containment policy"):
        ContainmentPolicy.coerce("delete-everything")


def test_nested_is_the_default():
    assert ReconciliationPolicy().containment_policy is ContainmentPolicy.NESTED


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("containment_threshold", 0.0),
        ("containment_threshold", 1.5),
        ("iou_duplicate", 0.0),
        ("min_child_area_m2", -1.0),
    ],
)
def test_invalid_policy_parameters_are_rejected(field, value):
    with pytest.raises(ValueError):
        ReconciliationPolicy(**{field: value})


# --- decisions --------------------------------------------------------------


def test_nested_policy_retains_the_contained_footprint():
    warehouse, shop = box(0, 0, 900, 400), box(100, 100, 108, 108)
    drop, reason = decide(classify(shop, warehouse, POLICY), POLICY)
    assert drop is False
    assert "nested" in reason


def test_absorb_policy_drops_the_contained_footprint():
    policy = ReconciliationPolicy(containment_policy=ContainmentPolicy.ABSORB)
    warehouse, shop = box(0, 0, 900, 400), box(100, 100, 108, 108)
    drop, reason = decide(classify(shop, warehouse, policy), policy)
    assert drop is True
    assert "absorb" in reason


def test_absorb_when_coincident_keeps_genuine_nesting():
    """A small roof inside a large complex is not absorbed by outline coincidence."""
    policy = ReconciliationPolicy(containment_policy=ContainmentPolicy.ABSORB_WHEN_COINCIDENT)
    drop, reason = decide(classify(box(100, 100, 108, 108), box(0, 0, 900, 400), policy), policy)
    assert drop is False
    assert "do not coincide" in reason


def test_absorb_when_coincident_absorbs_largely_coincident_outlines():
    """Outlines that mostly agree are absorbed; 900x400 in 900x900 (IoU .44) is not.

    The gate is deliberately stricter than plain containment, so a genuinely
    smaller structure inside a larger one survives even in coincident mode.
    """
    policy = ReconciliationPolicy(containment_policy=ContainmentPolicy.ABSORB_WHEN_COINCIDENT)

    # For nested boxes sharing an origin, IoU equals the area ratio, so 0.78 sits
    # just below the 0.80 duplicate gate while still being 78% coincident in scale.
    coincident = classify(box(0, 0, 100, 100), box(0, 0, 100, 78), policy)
    assert coincident.kind == CLASS_CONTAINMENT
    assert coincident.metrics.area_ratio >= policy.coincident_area_ratio
    drop, reason = decide(coincident, policy)
    assert drop is True
    assert "coincident" in reason

    nested_majority = classify(box(0, 0, 900, 900), box(0, 0, 900, 400), policy)
    assert nested_majority.metrics.area_ratio < policy.coincident_area_ratio
    assert decide(nested_majority, policy)[0] is False


def test_min_child_area_protects_small_structures_from_absorption():
    """An 8 m shed is absorbed by default; a floor above its area protects it."""
    classified = classify(box(100, 100, 108, 108), box(0, 0, 900, 400), ReconciliationPolicy())
    # The shed is 64 m2, so a 100 m2 floor protects it.
    floor = ReconciliationPolicy(containment_policy=ContainmentPolicy.ABSORB, min_child_area_m2=100.0)
    drop, reason = decide(classified, floor)
    assert drop is False
    assert "below the absorption floor" in reason

    no_floor = ReconciliationPolicy(containment_policy=ContainmentPolicy.ABSORB)
    assert decide(classified, no_floor)[0] is True


def test_duplicates_are_dropped_under_every_policy():
    for mode in ContainmentPolicy:
        policy = ReconciliationPolicy(containment_policy=mode)
        drop, _ = decide(classify(box(0, 0, 10, 10), box(0.05, 0.05, 10.05, 10.05), policy), policy)
        assert drop is True, mode


# --- merge integration ------------------------------------------------------


def _small_buildings(count: int = 300):
    return [
        _rec(box((i % 50) * 20.0, (i // 50) * 20.0, (i % 50) * 20.0 + 8, (i // 50) * 20.0 + 8), f"tile:s{i}")
        for i in range(count)
    ]


def test_nested_default_retains_the_whole_mixed_fabric():
    warehouse = _rec(box(0, 0, 900, 400), "tile:warehouse")
    payload = [*_small_buildings(), warehouse]
    log = ReconciliationLog()
    result = merge_footprint_records([], payload, reconciliation=log)
    assert len(result) == len(payload)
    assert log.containments_seen > 0
    assert log.nested_retained == log.containments_seen
    assert log.absorbed == 0


def test_absorb_policy_reproduces_the_pre_v1_8_0_result():
    warehouse = _rec(box(0, 0, 900, 400), "tile:warehouse")
    payload = [*_small_buildings(), warehouse]
    policy = ReconciliationPolicy(containment_policy=ContainmentPolicy.ABSORB)
    absorbed = merge_footprint_records([], payload, policy=policy)
    assert len(absorbed) < len(payload)
    assert len(absorbed) == 31  # the v1.7.0 figure for this fixture, reproduced exactly


def test_containment_decision_between_two_large_footprints_matches_iou_view():
    """900x400 inside 900x900: 100% contained but only 44% coincident."""
    large, inner = _rec(box(0, 0, 900, 900), "tile:large"), _rec(box(0, 0, 900, 400), "tile:inner")
    assert len(merge_footprint_records([], [large, inner])) == 2
    absorbing = ReconciliationPolicy(containment_policy=ContainmentPolicy.ABSORB)
    assert len(merge_footprint_records([], [large, inner], policy=absorbing)) == 1


# --- phase A: authoritative records ----------------------------------------


def test_inferred_footprint_enveloping_osm_is_retained_and_tagged():
    osm = FootprintRecord(box(100, 100, 108, 108), "osm", "osm:shop", None, "ODbL-1.0")
    warehouse = _rec(box(0, 0, 900, 400), "g:warehouse")
    log = ReconciliationLog()

    result = merge_footprint_records([osm], [warehouse], reconciliation=log)
    survivor = next(record for record in result if record.source_id == "g:warehouse")
    assert survivor.attributes["contains_authoritative"] == ["osm:shop"]
    assert any(record.provider == "osm" for record in result)
    assert log.inferred_retained_over_authoritative == 1
    assert log.contains_authoritative_tags == ["g:warehouse"]


def test_authoritative_records_are_never_dropped():
    osm = FootprintRecord(box(0, 0, 10, 10), "osm", "osm:1", None, "ODbL-1.0")
    inferred = [
        _rec(box(0.02, 0.02, 10.02, 10.02), "g:dup"),  # duplicate
        _rec(box(0, 0, 900, 400), "g:envelope"),  # contains the OSM footprint
        _rec(box(1, 1, 3, 3), "g:inside"),  # inside the OSM footprint
    ]
    result = merge_footprint_records([osm], inferred)
    assert any(record.source_id == "osm:1" for record in result)


def test_inferred_footprint_inside_authoritative_is_still_dropped():
    osm = FootprintRecord(box(0, 0, 900, 400), "osm", "osm:complex", None, "ODbL-1.0")
    inside = _rec(box(100, 100, 108, 108), "g:shed")
    log = ReconciliationLog()
    result = merge_footprint_records([osm], [inside], reconciliation=log)
    assert [record.source_id for record in result] == ["osm:complex"]
    assert log.authoritative_contained_dropped == 1


# --- provenance -------------------------------------------------------------


def test_every_drop_has_an_auditable_record():
    osm = FootprintRecord(box(0, 0, 10, 10), "osm", "osm:1", None, "ODbL-1.0")
    inferred = [
        _rec(box(0.02, 0.02, 10.02, 10.02), "g:dup"),
        _rec(box(0, 0, 900, 400), "g:envelope"),
        _rec(box(50, 50, 58, 58), "g:small"),
        _rec(box(500, 500, 508, 508), "g:distinct"),
    ]
    dedup = DeduplicationReport()
    log = ReconciliationLog()
    merge_footprint_records([osm], inferred, report=dedup, reconciliation=log)

    expected = dedup.dropped_as_duplicate + dedup.dropped_as_authoritative_duplicate
    accounted = log.duplicates_dropped + log.absorbed + log.authoritative_contained_dropped
    assert accounted == expected


def test_reconciliation_pairs_are_deterministic_and_fully_described():
    warehouse = _rec(box(0, 0, 900, 400), "tile:warehouse")
    payload = [*_small_buildings(40), warehouse]
    first, second = ReconciliationLog(), ReconciliationLog()
    merge_footprint_records([], payload, reconciliation=first)
    merge_footprint_records([], list(reversed(payload)), reconciliation=second)

    assert first.sorted_pairs() == second.sorted_pairs()

    pair = first.sorted_pairs()[0]
    assert set(pair) >= {
        "phase",
        "decision",
        "reason",
        "parent",
        "child",
        "class",
        "direction",
        "containment",
        "iou",
        "area_ratio",
    }
    assert pair["parent"]["area_m2"] >= pair["child"]["area_m2"]


def test_summary_counts_reconcile_with_each_other():
    warehouse = _rec(box(0, 0, 900, 400), "tile:warehouse")
    log = ReconciliationLog()
    merge_footprint_records([], [*_small_buildings(50), warehouse], reconciliation=log)
    summary = log.summary()
    assert summary["nested_retained"] + summary["absorbed"] == summary["containments_seen"]
    assert summary["record_count"] == len(log.records)


# --- builder + CLI exposure -------------------------------------------------


def test_builder_exposes_policy_flags_with_nested_default():
    import importlib.util

    spec = importlib.util.spec_from_file_location("policy_build", ROOT / "tools" / "cad-fond-de-plan-build.py")
    assert spec is not None and spec.loader is not None
    build = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(build)

    parser = build.build_parser()
    defaults = parser.parse_args([])
    assert defaults.containment_policy == "nested"
    assert defaults.containment_threshold == 0.88
    assert defaults.iou_duplicate == 0.80
    assert defaults.min_child_area_m2 == 0.0

    chosen = parser.parse_args(["--containment-policy", "absorb", "--min-child-area-m2", "25"])
    assert chosen.containment_policy == "absorb"
    assert chosen.min_child_area_m2 == 25.0

    with pytest.raises(SystemExit):
        parser.parse_args(["--containment-policy", "obliterate"])


def test_build_summary_embeds_reconciliation_and_audit():
    import importlib.util

    spec = importlib.util.spec_from_file_location("policy_build2", ROOT / "tools" / "cad-fond-de-plan-build.py")
    assert spec is not None and spec.loader is not None
    build = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(build)

    from footprint_providers import MicrosoftFootprintProvider

    warehouse = _rec(box(0, 0, 900, 400), "tile:warehouse")
    payload = [*_small_buildings(20), warehouse]
    dedup, log = DeduplicationReport(), ReconciliationLog()
    unique = merge_footprint_records([], payload, report=dedup, reconciliation=log)

    summary = build.build_footprint_summary(
        MicrosoftFootprintProvider(),
        "fixture",
        [ROOT / "tests" / "fixtures" / "google_open_buildings_sample.csv"],
        None,
        payload,
        unique,
        unique,
        None,
        None,
        "not-requested",
        dedup,
        log,
    )
    reconciliation = summary["reconciliation"]
    assert reconciliation["policy"]["containment_policy"] == "nested"
    assert reconciliation["nested_retained"] > 0
    assert reconciliation["audit"]["consistent"] is True
    assert reconciliation["pairs"]
