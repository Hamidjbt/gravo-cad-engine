#!/usr/bin/env python3
"""Explicit reconciliation policy for overlapping building footprints.

Why this module exists
----------------------
Similarity between two footprints used to be scored as ``intersection ÷ min(area)``.
That quantity is maximal for a *containment* relationship (an 8 m shop inside a
900 × 400 m warehouse scores 1.0) and for a genuine *duplicate* (0.9996). The two
phenomena are indistinguishable to it, so no threshold on that score could ever
tell them apart — raising or lowering it only moved the error.

This module asks three separate questions instead:

* ``iou = ∩ / ∪``            — "are these the same building?"
* ``containment = ∩ / min``  — "is one inside the other?"
* ``area_ratio = min / max`` — "how different in scale are they?"

Containment is then routed through a *declared* policy rather than silently
absorbed, and every decision is recorded so a caller can audit it.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any

from shapely.geometry import Polygon

POLICY_NESTED = "nested"
POLICY_ABSORB = "absorb"
POLICY_ABSORB_WHEN_COINCIDENT = "absorb_when_coincident"
CONTAINMENT_POLICIES = (POLICY_NESTED, POLICY_ABSORB, POLICY_ABSORB_WHEN_COINCIDENT)

CLASS_DUPLICATE = "duplicate"
CLASS_CONTAINMENT = "containment"
CLASS_DISTINCT = "distinct"

DIRECTION_EQUIVALENT = "equivalent"
DIRECTION_CONTAINS = "contains"
DIRECTION_CONTAINED_BY = "contained_by"


class ContainmentPolicy(str, Enum):
    """What to do when one footprint is nested inside another.

    ``NESTED`` is the default because retention is non-destructive and
    reversible: a nested pair can always be absorbed later with one flag, while a
    silently deleted building cannot be recovered from the output.
    """

    NESTED = POLICY_NESTED
    ABSORB = POLICY_ABSORB
    ABSORB_WHEN_COINCIDENT = POLICY_ABSORB_WHEN_COINCIDENT

    @classmethod
    def coerce(cls, value: Any) -> ContainmentPolicy:
        if isinstance(value, cls):
            return value
        try:
            return cls(str(value).strip().lower())
        except ValueError as error:
            raise ValueError(
                f"Unknown containment policy {value!r}; choose from {', '.join(CONTAINMENT_POLICIES)}"
            ) from error


@dataclass(frozen=True)
class ReconciliationPolicy:
    """Declared thresholds for classifying and acting on overlapping footprints.

    ``containment_threshold`` (0.88) reproduces the historical gate, so
    ``ABSORB`` reproduces pre-v1.8.0 output exactly.
    """

    containment_policy: ContainmentPolicy = ContainmentPolicy.NESTED
    containment_threshold: float = 0.88
    iou_duplicate: float = 0.80
    authoritative_containment_threshold: float = 0.62
    coincident_area_ratio: float = 0.70
    coincident_iou: float = 0.50
    min_child_area_m2: float = 0.0

    def __post_init__(self) -> None:
        if not 0.0 < self.containment_threshold <= 1.0:
            raise ValueError("containment_threshold must be in (0, 1]")
        if not 0.0 < self.iou_duplicate <= 1.0:
            raise ValueError("iou_duplicate must be in (0, 1]")
        if not 0.0 < self.authoritative_containment_threshold <= 1.0:
            raise ValueError("authoritative_containment_threshold must be in (0, 1]")
        if self.min_child_area_m2 < 0.0:
            raise ValueError("min_child_area_m2 must be >= 0")

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["containment_policy"] = self.containment_policy.value
        return payload


@dataclass(frozen=True)
class OverlapMetrics:
    """Scale-aware description of one footprint pair."""

    intersection_m2: float
    union_m2: float
    containment: float
    iou: float
    area_ratio: float
    larger_area_m2: float
    smaller_area_m2: float

    @classmethod
    def between(cls, first: Polygon, second: Polygon) -> OverlapMetrics:
        first_area, second_area = float(first.area), float(second.area)
        intersection = float(first.intersection(second).area)
        smaller = min(first_area, second_area)
        larger = max(first_area, second_area)
        union = first_area + second_area - intersection
        denom = max(smaller, 1e-8)
        return cls(
            intersection_m2=intersection,
            union_m2=union,
            containment=intersection / denom,
            iou=intersection / union if union > 1e-8 else 0.0,
            area_ratio=smaller / larger if larger > 1e-8 else 0.0,
            larger_area_m2=larger,
            smaller_area_m2=smaller,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "intersection_m2": round(self.intersection_m2, 3),
            "containment": round(self.containment, 6),
            "iou": round(self.iou, 6),
            "area_ratio": round(self.area_ratio, 6),
            "larger_area_m2": round(self.larger_area_m2, 3),
            "smaller_area_m2": round(self.smaller_area_m2, 3),
        }


@dataclass(frozen=True)
class Classification:
    """Outcome of comparing two footprints, with the metrics behind it."""

    kind: str
    direction: str
    metrics: OverlapMetrics

    @property
    def is_duplicate(self) -> bool:
        return self.kind == CLASS_DUPLICATE

    @property
    def is_containment(self) -> bool:
        return self.kind == CLASS_CONTAINMENT

    def to_dict(self) -> dict[str, Any]:
        return {"class": self.kind, "direction": self.direction, **self.metrics.to_dict()}


def _direction(candidate_area: float, known_area: float, tolerance: float = 0.05) -> str:
    """Describe the candidate relative to the known record, decided by scale.

    A tolerance keeps near-equal areas reported as equivalent rather than
    inventing a parent/child relation out of rounding.
    """
    if known_area <= 0 or candidate_area <= 0:
        return DIRECTION_EQUIVALENT
    ratio = candidate_area / known_area
    if ratio > 1.0 + tolerance:
        return DIRECTION_CONTAINS
    if ratio < 1.0 - tolerance:
        return DIRECTION_CONTAINED_BY
    return DIRECTION_EQUIVALENT


def classify(candidate: Polygon, known: Polygon, policy: ReconciliationPolicy) -> Classification:
    """Classify a pair as duplicate, containment, or distinct.

    Duplicate is tested first: IoU separates the two phenomena by orders of
    magnitude (0.9993 versus 0.0002 on representative geometry), so the ordering
    is not a tuning detail.
    """
    metrics = OverlapMetrics.between(candidate, known)
    direction = _direction(float(candidate.area), float(known.area))
    if metrics.iou >= policy.iou_duplicate:
        kind = CLASS_DUPLICATE
    elif metrics.containment >= policy.containment_threshold:
        kind = CLASS_CONTAINMENT
    else:
        kind = CLASS_DISTINCT
    return Classification(kind=kind, direction=direction, metrics=metrics)


def decide(
    classification: Classification,
    policy: ReconciliationPolicy,
    *,
    phase: str = "dedup",
) -> tuple[bool, str]:
    """Return ``(drop_candidate, reason)`` for one classified pair.

    ``phase="dedup"`` compares two inferred/merged records. ``phase="authority"``
    compares an inferred record against an authoritative one, where the
    authoritative record is never dropped under any policy.
    """
    metrics = classification.metrics
    if classification.is_duplicate:
        return True, "duplicate outlines (high IoU)"

    if not classification.is_containment:
        return False, "distinct footprints"

    if policy.min_child_area_m2 and metrics.smaller_area_m2 < policy.min_child_area_m2:
        # Protects small structures from ABSORB only. A duplicate is a duplicate at
        # any size: two overlapping 6 m2 sheds are still one shed recorded twice.
        return False, f"smaller footprint {metrics.smaller_area_m2:.1f} m2 is below the absorption floor"

    if phase == "authority":
        if classification.direction == DIRECTION_CONTAINED_BY:
            # The inferred footprint sits inside an authoritative one: the
            # authoritative record already covers it.
            return True, "inferred footprint is inside an authoritative record"
        # The inferred footprint ENVELOPS an authoritative one. Historically it
        # was discarded wholesale, deleting buildings that genuinely exist.
        return False, "inferred footprint contains an authoritative record"

    mode = policy.containment_policy
    if mode is ContainmentPolicy.NESTED:
        return False, "nested footprint retained (policy=nested)"
    if mode is ContainmentPolicy.ABSORB:
        return True, "absorbed by enclosing footprint (policy=absorb)"
    # ABSORB_WHEN_COINCIDENT: absorb only when the outlines plausibly describe the
    # same structure rather than a genuine enclosure.
    if metrics.area_ratio >= policy.coincident_area_ratio or metrics.iou >= policy.coincident_iou:
        return True, "absorbed: outlines are largely coincident (policy=absorb_when_coincident)"
    return False, "nested footprint retained: outlines do not coincide"


@dataclass
class ReconciliationRecord:
    """One auditable reconciliation decision."""

    phase: str
    decision: str
    reason: str
    parent_source_id: str
    parent_provider: str
    parent_area_m2: float
    child_source_id: str
    child_provider: str
    child_area_m2: float
    classification: Classification
    dropped_source_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "decision": self.decision,
            "reason": self.reason,
            "dropped_source_id": self.dropped_source_id,
            "parent": {
                "source_id": self.parent_source_id,
                "provider": self.parent_provider,
                "area_m2": round(self.parent_area_m2, 3),
            },
            "child": {
                "source_id": self.child_source_id,
                "provider": self.child_provider,
                "area_m2": round(self.child_area_m2, 3),
            },
            **self.classification.to_dict(),
        }


@dataclass
class ReconciliationLog:
    """Ordered, serialisable record of every non-distinct pair decision."""

    policy: ReconciliationPolicy = field(default_factory=ReconciliationPolicy)
    records: list[ReconciliationRecord] = field(default_factory=list)
    duplicates_dropped: int = 0
    containments_seen: int = 0
    nested_retained: int = 0
    absorbed: int = 0
    authoritative_contained_dropped: int = 0
    inferred_retained_over_authoritative: int = 0
    contains_authoritative_tags: list[str] = field(default_factory=list)

    def add(self, record: ReconciliationRecord) -> None:
        """Fold one pair decision into the counters.

        A drop always removes the *enclosed* footprint (a higher-IoU duplicate of a
        larger record is itself the smaller one in practice; for near-equal
        duplicates the recorded child is the second record examined). In authority
        phase the drop is the inferred record sitting inside an authoritative one.
        """
        self.records.append(record)
        dropped = record.decision == "dropped"

        if record.classification.is_duplicate:
            self.duplicates_dropped += int(dropped)

        if record.phase == "authority":
            # Authority never consults the containment policy: an inferred record
            # inside an authoritative one is always superseded, while one that
            # encloses authoritative records is always retained.
            if record.classification.is_containment:
                self.containments_seen += 1
                if dropped:
                    self.authoritative_contained_dropped += 1
                else:
                    self.inferred_retained_over_authoritative += 1
            return

        if record.classification.is_containment:
            self.containments_seen += 1
            if dropped:
                self.absorbed += 1
            else:
                self.nested_retained += 1

    def summary(self) -> dict[str, Any]:
        return {
            "policy": self.policy.to_dict(),
            "duplicates_dropped": self.duplicates_dropped,
            "containments_seen": self.containments_seen,
            "nested_retained": self.nested_retained,
            "absorbed": self.absorbed,
            "authoritative_contained_dropped": self.authoritative_contained_dropped,
            "inferred_retained_over_authoritative": self.inferred_retained_over_authoritative,
            "contains_authoritative_tags": sorted(self.contains_authoritative_tags),
            "record_count": len(self.records),
        }

    def sorted_pairs(self) -> list[dict[str, Any]]:
        """Deterministic ordering, so reports are reproducible."""
        return [
            record.to_dict()
            for record in sorted(
                self.records, key=lambda item: (item.parent_source_id, item.child_source_id, item.phase)
            )
        ]
