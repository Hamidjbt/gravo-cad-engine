#!/usr/bin/env python3
"""Workspace storage hygiene: keep evidence, delete only what is regenerable.

The rule this module encodes
----------------------------
Storage pressure comes from a small number of *regenerable* file classes (byte-code
caches, test caches, throwaway virtualenvs, downloaded tiles), while the files that
actually matter — tracked source, Git history, bundles, tests, architecture maps,
deliverables and installer scripts — are comparatively small and irreplaceable.

So the default action is **class-based, not age-based**: a known-disposable pattern
inside a known-disposable location is removed; everything else is preserved. Age is
only ever used as a second gate for large data that is *retained by default*.

Three tiers
-----------
``PROTECTED``
    Never removed by any mode. Checked first, and it wins over every other rule.
``DISPOSABLE``
    Removed by ``prune`` without further prompting. All of it is regenerable:
    caches, byte-code, build directories, orphaned scratch virtualenvs.
``RETAINED``
    Downloaded tiles and run outputs. Large, but they are evidence for a report or
    a reproduction. Removed only with ``--include-data`` *and* older than
    ``--max-age-days``.

Usage
-----
    python tools/workspace_hygiene.py report            # what is on disk, by tier
    python tools/workspace_hygiene.py prune             # remove tier DISPOSABLE
    python tools/workspace_hygiene.py prune --dry-run   # show the plan, change nothing
    python tools/workspace_hygiene.py prune --include-data --max-age-days 14
    python tools/workspace_hygiene.py check             # exit 1 if caches are present

Exit codes: 0 clean/ok, 1 attention required (``check`` only).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

# --- tier 1: protected ------------------------------------------------------
# Matched against the path *relative to the scan root*, POSIX separators.
PROTECTED_DIR_NAMES = frozenset(
    {
        ".git",
        "site-plan",  # hand-written earlier deliverable; not reproducible from this repo
        "uploads",  # user-supplied reference aerials
        "motion-projects",
    }
)
PROTECTED_SUFFIXES = frozenset({".bundle", ".md", ".docx", ".pptx", ".xlsx", ".sh"})
PROTECTED_FILE_NAMES = frozenset({"INSTALL_IN_NEW_CHAT.sh", ".gitignore", "Makefile"})
# Source and test suites are protected structurally, by directory, not by suffix:
# a blanket "*.py" rule would also shield nothing useful (no *.py is ever
# disposable) while a suffix rule for suites is impossible to express safely.
PROTECTED_DIRS = frozenset({"tests", "tools", "server", "skills", "schemas", "docs"})

# --- tier 2: disposable -----------------------------------------------------
CACHE_DIR_NAMES = frozenset(
    {
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
        ".hypothesis",
        ".ipynb_checkpoints",
    }
)
BUILD_DIR_NAMES = frozenset({"build", "dist"})
DISPOSABLE_FILE_GLOBS = ("*.pyc", "*.pyo", "*.orig", "*.rej", "*.tmp", "*.swp")

# Scratch virtualenvs we create are prefixed so they are identifiable; anything
# else in a scratch root is left alone because we cannot prove we made it.
SCRATCH_VENV_PREFIXES = ("cad-fond-", "trial", "rv", "hygiene-")
SCRATCH_ROOTS = ("/var/tmp",)

# --- tier 3: retained by default -------------------------------------------
DATA_DIR_NAMES = frozenset({"source", "cache", "site"})
DATA_FILE_PATTERNS = (
    "*.csv.gz",
    "*.osm",
    "*.pbf",
    "*.gpkg",
    "*.tif",
    "*.dxf",
    "*.svg",
    "*.pdf",
)
DATA_MIN_AGE_DAYS = 14


@dataclass
class Entry:
    path: Path
    tier: str
    reason: str
    size: int = 0

    def as_dict(self) -> dict:
        return {
            "path": str(self.path),
            "tier": self.tier,
            "reason": self.reason,
            "bytes": self.size,
        }


@dataclass
class Plan:
    disposable: list[Entry] = field(default_factory=list)
    retained: list[Entry] = field(default_factory=list)
    protected_hits: int = 0

    @property
    def disposable_bytes(self) -> int:
        return sum(entry.size for entry in self.disposable)

    @property
    def retained_bytes(self) -> int:
        return sum(entry.size for entry in self.retained)

    def as_dict(self) -> dict:
        return {
            "disposable": [entry.as_dict() for entry in self.disposable],
            "retained": [entry.as_dict() for entry in self.retained],
            "disposable_bytes": self.disposable_bytes,
            "retained_bytes": self.retained_bytes,
            "protected_skipped": self.protected_hits,
        }


def _tree_size(path: Path) -> int:
    """Bytes under ``path`` without following symlinks or blowing up on races."""
    if path.is_file():
        try:
            return path.stat().st_size
        except OSError:
            return 0
    total = 0
    for root, _pruned, filenames in os.walk(path, onerror=lambda _: None):
        for name in filenames:
            try:
                total += (Path(root) / name).lstat().st_size
            except OSError:
                continue
    return total


def is_disposable_name(name: str) -> bool:
    """True for a directory or file name that is regenerable wherever it appears.

    This is checked *before* protection: ``tools/__pycache__`` is a cache inside a
    protected source tree and must still be prunable, while ``tests/`` itself is not.
    """
    if name in CACHE_DIR_NAMES or name in BUILD_DIR_NAMES or name.endswith(".egg-info"):
        return True
    return any(Path(name).match(pattern) for pattern in DISPOSABLE_FILE_GLOBS)


def _disposable_dir_reason(name: str) -> str | None:
    """Why a directory named ``name`` is regenerable, or None. Checked before protection."""
    if name in CACHE_DIR_NAMES:
        return f"cache directory {name}"
    if name in BUILD_DIR_NAMES:
        return f"generated build directory {name}"
    if name.endswith(".egg-info"):
        return "editable-install metadata"
    return None


def _is_protected(rel: Path) -> bool:
    """True when a path must never be removed.

    The innermost component decides: a cache name is disposable even under a
    protected parent, but a protected name is never disposable.
    """
    if is_disposable_name(rel.name):
        return False
    if any(part in PROTECTED_DIR_NAMES or part in PROTECTED_DIRS for part in rel.parts):
        return True
    return rel.name in PROTECTED_FILE_NAMES or rel.suffix in PROTECTED_SUFFIXES


def _older_than(path: Path, days: float) -> bool:
    try:
        newest = path.stat().st_mtime
    except OSError:
        return False
    if path.is_dir():
        for root, _dirs, files in os.walk(path, onerror=lambda _: None):
            for name in files:
                try:
                    newest = max(newest, (Path(root) / name).stat().st_mtime)
                except OSError:
                    continue
    return (time.time() - newest) > days * 86400.0


def scan(
    root: Path,
    *,
    include_data: bool = False,
    max_age_days: float = DATA_MIN_AGE_DAYS,
    scratch_roots: tuple[str, ...] = SCRATCH_ROOTS,
) -> Plan:
    """Classify storage under ``root`` (and known scratch roots) into a plan."""
    root = Path(root).expanduser().resolve()
    plan = Plan()

    for dirpath, dirnames, filenames in os.walk(root, topdown=True, onerror=lambda _: None):
        here = Path(dirpath)
        rel_dir = here.relative_to(root)

        descend: list[str] = []
        for name in list(dirnames):
            rel = rel_dir / name
            candidate = here / name
            reason = _disposable_dir_reason(name)
            if reason is not None:
                plan.disposable.append(Entry(candidate, "disposable", reason, _tree_size(candidate)))
                continue  # pruned whole; never descend into it
            if include_data and name in DATA_DIR_NAMES and _older_than(candidate, max_age_days):
                plan.retained.append(
                    Entry(
                        candidate, "retained", f"downloaded data older than {max_age_days:g} d", _tree_size(candidate)
                    )
                )
                descend.append(name)  # retained, but its caches are still prunable
                continue
            if _is_protected(rel):
                plan.protected_hits += 1
                descend.append(name)  # protected, but caches *inside* it are prunable
                continue
            descend.append(name)
        dirnames[:] = descend

        for name in filenames:
            rel = rel_dir / name
            if _is_protected(rel):
                plan.protected_hits += 1
                continue
            candidate = here / name
            if any(candidate.match(pattern) for pattern in DISPOSABLE_FILE_GLOBS):
                plan.disposable.append(Entry(candidate, "disposable", "stray temporary file", _tree_size(candidate)))

    for scratch in scratch_roots:
        base = Path(scratch)
        if not base.is_dir():
            continue
        for child in base.iterdir():
            if child.name.startswith("systemd-private"):
                continue
            if not child.is_dir():
                continue
            if not child.name.startswith(SCRATCH_VENV_PREFIXES):
                continue
            if not (child / "bin" / "python").exists() and not (child / "pyvenv.cfg").exists():
                continue
            plan.disposable.append(
                Entry(child, "disposable", "throwaway virtualenv in scratch space", _tree_size(child))
            )
    return plan


def prune(plan: Plan, *, dry_run: bool = False) -> dict:
    """Remove the disposable tier. Refuses to touch a protected path, ever."""
    removed: list[dict] = []
    failed: list[dict] = []
    freed = 0
    for entry in plan.disposable:
        rel_guard = Path(entry.path.name)
        if _is_protected(rel_guard):
            failed.append({"path": str(entry.path), "error": "refused: path is protected"})
            continue
        if dry_run:
            removed.append({"path": str(entry.path), "bytes": entry.size, "reason": entry.reason, "dry_run": True})
            freed += entry.size
            continue
        try:
            if entry.path.is_dir() and not entry.path.is_symlink():
                shutil.rmtree(entry.path)
            elif entry.path.exists() or entry.path.is_symlink():
                entry.path.unlink()
            removed.append({"path": str(entry.path), "bytes": entry.size, "reason": entry.reason})
            freed += entry.size
        except OSError as exc:  # pragma: no cover - environmental
            failed.append({"path": str(entry.path), "error": str(exc)})
    return {
        "removed": removed,
        "failed": failed,
        "freed_bytes": freed,
        "dry_run": dry_run,
        "remaining_disposable": len(plan.disposable) - len(removed) - len(failed),
    }


def human_bytes(count: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if abs(count) < 1024.0 or unit == "GiB":
            return f"{count:.1f} {unit}"
        count /= 1024.0
    return f"{count:.1f} GiB"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prune regenerable storage, preserve evidence.")
    parser.add_argument("action", choices=("report", "prune", "check"))
    parser.add_argument("--root", default=str(Path(__file__).resolve().parents[1]), help="tree to scan")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--dry-run", action="store_true", help="plan only; delete nothing")
    parser.add_argument("--include-data", action="store_true", help="also prune stale downloaded data")
    parser.add_argument("--max-age-days", type=float, default=DATA_MIN_AGE_DAYS)
    parser.add_argument("--no-scratch", action="store_true", help="ignore /var/tmp virtualenvs")
    args = parser.parse_args(argv)

    plan = scan(
        Path(args.root),
        include_data=args.include_data,
        max_age_days=args.max_age_days,
        scratch_roots=() if args.no_scratch else SCRATCH_ROOTS,
    )

    if args.action == "check":
        ok = not plan.disposable
        if args.json:
            print(json.dumps({"ok": ok, **plan.as_dict()}, indent=2))
        else:
            print("clean" if ok else f"{len(plan.disposable)} disposable item(s) present")
            for entry in plan.disposable:
                print(f"  {entry.reason:38} {human_bytes(entry.size):>10}  {entry.path}")
        return 0 if ok else 1

    if args.action == "report":
        payload = plan.as_dict()
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            print(f"protected items skipped : {plan.protected_hits}")
            print(
                f"disposable              : {len(plan.disposable):>3} items  {human_bytes(plan.disposable_bytes):>10}"
            )
            for entry in plan.disposable:
                print(f"    {entry.reason:38} {human_bytes(entry.size):>10}  {entry.path}")
            print(f"retained data           : {len(plan.retained):>3} items  {human_bytes(plan.retained_bytes):>10}")
            for entry in plan.retained:
                print(f"    {entry.reason:38} {human_bytes(entry.size):>10}  {entry.path}")
        return 0

    result = prune(plan, dry_run=args.dry_run)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        verb = "would free" if args.dry_run else "freed"
        for item in result["removed"]:
            print(f"  {'-' if not args.dry_run else '~'} {human_bytes(item['bytes']):>10}  {item['path']}")
        for item in result["failed"]:
            print(f"  ! {item['path']}: {item['error']}", file=sys.stderr)
        print(f"{verb} {human_bytes(result['freed_bytes'])} across {len(result['removed'])} item(s)")
    return 0 if not result["failed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
