#!/usr/bin/env python3
"""Versioned report schema helpers for Fond de Plan provenance.

The engine report is a machine contract: downstream tools, QA, and agents read
``fond-de-plan-report.json``. This module owns its version, its provenance block
(engine identity plus input-file digests), and the ``summary``/``full`` trim
policy so large per-feature arrays stay out of archival reports unless asked for.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

REPORT_SCHEMA_VERSION = "1.0"
REPORT_DETAIL_MODES = ("summary", "full")

#: Keys carrying per-feature arrays that grow with source row count.
LARGE_ARRAY_PATHS = (
    ("footprints", "output_provenance"),
    ("footprints", "source_feature_thresholds"),
)


def package_version(default: str = "unknown") -> str:
    """Return the installed distribution version, falling back to a static value."""
    try:
        from importlib.metadata import PackageNotFoundError, version

        try:
            return version("cad-fond-de-plan")
        except PackageNotFoundError:
            return default
    except Exception:  # noqa: BLE001 - metadata lookup must never break a build
        return default


def file_digest(path: Path | str | None, *, algorithm: str = "sha256") -> dict[str, Any] | None:
    """Return ``{"name", "bytes", "sha256"}`` for a file, or ``None`` if unavailable."""
    if path is None:
        return None
    candidate = Path(path)
    try:
        if not candidate.is_file():
            return None
        digest = hashlib.new(algorithm)
        with candidate.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return {"name": candidate.name, "bytes": candidate.stat().st_size, algorithm: digest.hexdigest()}
    except OSError:
        return None


def input_provenance(paths: list[Path | str] | None, *, role: str = "footprint_source") -> list[dict[str, Any]]:
    """Digest a list of input files, skipping any that cannot be read."""
    entries: list[dict[str, Any]] = []
    for raw in paths or []:
        digest = file_digest(raw)
        if digest is not None:
            entries.append({"role": role, **digest})
    return entries


def provenance_block(
    *,
    engine_name: str = "cad-fond-de-plan",
    engine_version: str | None = None,
    generated_at: str | None = None,
    inputs: list[dict[str, Any]] | None = None,
    parameters: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the ``report["provenance"]`` payload."""
    return {
        "engine": engine_name,
        "engine_version": engine_version or package_version(),
        "report_schema_version": REPORT_SCHEMA_VERSION,
        "report_detail": "full",
        "generated_at_utc": generated_at,
        "inputs": inputs or [],
        "parameters": parameters or {},
    }


def _trim(node: Any, detail: str) -> Any:
    if detail == "full" or not isinstance(node, dict):
        return node
    trimmed = dict(node)
    for section, key in LARGE_ARRAY_PATHS:
        block = trimmed.get(section)
        if isinstance(block, dict) and key in block:
            block = dict(block)
            entries = block.pop(key) or []
            block[f"{key}_omitted"] = {
                "reason": "report_detail=summary",
                "count": len(entries),
                "hint": "re-run with --report-detail full for per-feature provenance",
            }
            trimmed[section] = block
    return trimmed


def apply_detail(report: dict[str, Any], detail: str = "full") -> dict[str, Any]:
    """Return ``report`` trimmed to the requested detail level."""
    if detail not in REPORT_DETAIL_MODES:
        raise ValueError(f"detail must be one of {REPORT_DETAIL_MODES}")
    provenance = dict(report.get("provenance") or {})
    provenance["report_detail"] = detail
    trimmed = _trim(report, detail)
    trimmed["provenance"] = provenance
    return trimmed


def is_deterministic(
    report_a: dict[str, Any], report_b: dict[str, Any], *, volatile: tuple[str, ...] = ("generated_at_utc",)
) -> bool:
    """True when two reports match apart from declared volatile provenance fields."""
    import json

    def strip(report: dict[str, Any]) -> dict[str, Any]:
        provenance = dict(report.get("provenance") or {})
        for key in volatile:
            provenance.pop(key, None)
        return {**report, "provenance": provenance}

    return json.dumps(strip(report_a), sort_keys=True) == json.dumps(strip(report_b), sort_keys=True)
