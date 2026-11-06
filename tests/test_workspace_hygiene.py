"""Tests for the workspace hygiene protocol (v1.8.1).

The protocol's only job is to be *safe*: never delete something that cannot be
regenerated. These tests therefore concentrate on protection, not on deletion.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import workspace_hygiene as hygiene  # noqa: E402

# --- the classification contract -------------------------------------------


@pytest.mark.parametrize(
    ("rel", "expected", "why"),
    [
        (".git/config", True, "git history"),
        ("site-plan/fond-de-plan-cad.dxf", True, "earlier deliverable"),
        ("uploads/aerial.jpeg", True, "user-supplied reference"),
        ("motion-projects/deck.html", True, "unrelated user project"),
        ("gravo-cad-engine-v1.8.1.bundle", True, "release bundle"),
        ("DEVELOPMENT_ARCHITECTURE_MAP.md", True, "architecture map"),
        ("v1.8.0-VERIFICATION.md", True, "verification record"),
        ("INSTALL_IN_NEW_CHAT.sh", True, "installer script"),
        ("tests/test_reconciliation.py", True, "test suite"),
        ("tools/footprint_providers.py", True, "source"),
        ("server/mcp_server.py", True, "source"),
    ],
)
def test_protected_paths_are_never_removable(rel, expected, why):
    assert hygiene._is_protected(Path(rel)) is expected, why


@pytest.mark.parametrize(
    "rel",
    [
        "tools/__pycache__",
        "tools/__pycache__/footprint_providers.pyc",
        "server/__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
        "build/lib",
        "dist",
        "cad_fond_de_plan.egg-info",
    ],
)
def test_regenerable_paths_are_disposable(rel):
    assert hygiene._is_protected(Path(rel)) is False


def test_a_cache_inside_a_protected_tree_is_still_disposable():
    """The innermost name decides: `tools/` is protected, `tools/__pycache__` is not."""
    assert hygiene._is_protected(Path("tools")) is True
    assert hygiene._is_protected(Path("tools/__pycache__")) is False
    assert hygiene._is_protected(Path("tests")) is True
    assert hygiene._is_protected(Path("tests/__pycache__")) is False


# --- scanning ---------------------------------------------------------------


def _make(path: Path, content: str = "x", mtime_days_ago: float = 0.0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    if mtime_days_ago:
        import os

        stamp = time.time() - mtime_days_ago * 86400
        # Directories are aged too: the data-retention rule reads the newest mtime
        # under a directory, and a freshly created dir would otherwise look current.
        os.utime(path, (stamp, stamp))
        os.utime(path.parent, (stamp, stamp))
    return path


@pytest.fixture()
def workspace(tmp_path):
    _make(tmp_path / "tools" / "build.py")
    _make(tmp_path / "tools" / "__pycache__" / "build.cpython-313.pyc")
    _make(tmp_path / "tests" / "test_a.py")
    _make(tmp_path / ".pytest_cache" / "v" / "cache")
    _make(tmp_path / "build" / "lib" / "artifact.whl")
    _make(tmp_path / "site-plan" / "fond-de-plan-cad.dxf")
    _make(tmp_path / "uploads" / "aerial.jpeg")
    _make(tmp_path / "gravo-cad-engine-v1.8.1.bundle")
    _make(tmp_path / "DEVELOPMENT_ARCHITECTURE_MAP.md")
    _make(tmp_path / "INSTALL_IN_NEW_CHAT.sh")
    _make(tmp_path / "scratch.tmp")
    return tmp_path


def test_scan_separates_disposable_from_protected(workspace):
    plan = hygiene.scan(workspace, scratch_roots=())
    disposable = {entry.path.name for entry in plan.disposable}
    assert "__pycache__" in disposable
    assert ".pytest_cache" in disposable
    assert "build" in disposable
    assert "scratch.tmp" in disposable
    assert not {"site-plan", "uploads", "tests", "tools"} & disposable
    assert plan.protected_hits >= 6


def test_scan_ignores_a_missing_scratch_root(tmp_path):
    plan = hygiene.scan(tmp_path, scratch_roots=("/nonexistent-scratch-root",))
    assert plan.disposable == []


def test_scan_finds_throwaway_virtualenvs_but_leaves_foreign_dirs_alone(tmp_path):
    ours = tmp_path / "trialabc"
    ours.mkdir()
    _make(ours / "pyvenv.cfg")
    foreign = tmp_path / "someone-elses-data"
    foreign.mkdir()
    _make(foreign / "important.bin")
    systemd = tmp_path / "systemd-private-abc"
    systemd.mkdir()
    _make(systemd / "pyvenv.cfg")

    plan = hygiene.scan(tmp_path / "srcone", scratch_roots=(str(tmp_path),))
    asserted = {entry.path.name for entry in plan.disposable}
    assert "trialabc" in asserted
    assert "someone-elses-data" not in asserted
    assert "systemd-private-abc" not in asserted


# --- pruning ----------------------------------------------------------------


def test_prune_removes_only_the_disposable_tier(workspace):
    plan = hygiene.scan(workspace, scratch_roots=())
    result = hygiene.prune(plan)

    assert result["failed"] == []
    assert not (workspace / ".pytest_cache").exists()
    assert not (workspace / "build").exists()
    assert not (workspace / "tools" / "__pycache__").exists()
    assert not (workspace / "scratch.tmp").exists()

    # everything protected survives, byte for byte
    assert (workspace / "tools" / "build.py").is_file()
    assert (workspace / "tests" / "test_a.py").is_file()
    assert (workspace / "site-plan" / "fond-de-plan-cad.dxf").is_file()
    assert (workspace / "uploads" / "aerial.jpeg").is_file()
    assert (workspace / "gravo-cad-engine-v1.8.1.bundle").is_file()
    assert (workspace / "DEVELOPMENT_ARCHITECTURE_MAP.md").is_file()
    assert (workspace / "INSTALL_IN_NEW_CHAT.sh").is_file()


def test_dry_run_changes_nothing(workspace):
    before = sorted(p.relative_to(workspace).as_posix() for p in workspace.rglob("*"))
    plan = hygiene.scan(workspace, scratch_roots=())
    result = hygiene.prune(plan, dry_run=True)
    after = sorted(p.relative_to(workspace).as_posix() for p in workspace.rglob("*"))

    assert before == after
    assert result["freed_bytes"] > 0
    assert all(item["dry_run"] for item in result["removed"])


def test_prune_refuses_a_protected_entry_even_if_a_plan_contains_one(workspace):
    """Defence in depth: a corrupted plan must not be able to delete a protected path."""
    victim = workspace / "site-plan"
    plan = hygiene.Plan()
    plan.disposable.append(hygiene.Entry(victim, "disposable", "injected", 0))
    result = hygiene.prune(plan)

    assert victim.exists()
    assert result["failed"]
    assert "protected" in result["failed"][0]["error"]


def test_scan_then_prune_leaves_a_clean_tree(workspace):
    hygiene.prune(hygiene.scan(workspace, scratch_roots=()))
    assert hygiene.scan(workspace, scratch_roots=()).disposable == []


# --- retained data ----------------------------------------------------------


def test_stale_downloaded_data_is_retained_by_default(workspace):
    _make(workspace / "runs" / "source" / "microsoft-tile.csv.gz", mtime_days_ago=90)
    plan = hygiene.scan(workspace, scratch_roots=())
    assert not any(entry.path.name == "source" for entry in plan.disposable)


def test_stale_downloaded_data_is_offered_only_with_include_data(workspace):
    _make(workspace / "runs" / "source" / "microsoft-tile.csv.gz", mtime_days_ago=90)
    plan = hygiene.scan(workspace, include_data=True, max_age_days=14, scratch_roots=())
    retained = {entry.path.name for entry in plan.retained}
    assert "source" in retained

    result = hygiene.prune(plan)
    assert (workspace / "runs" / "source" / "microsoft-tile.csv.gz").exists(), "retained tier must not be pruned"
    assert result["removed"]


def test_recent_downloaded_data_is_not_even_offered(workspace):
    _make(workspace / "runs" / "source" / "fresh.csv.gz", mtime_days_ago=1)
    plan = hygiene.scan(workspace, include_data=True, max_age_days=14, scratch_roots=())
    assert not any(entry.path.name == "source" for entry in plan.retained)


# --- wiring -----------------------------------------------------------------


def test_makefile_routes_scratch_off_the_bounded_tmp_tmpfs():
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    assert "TMP_ROOT ?= /var/tmp" in makefile
    assert "TMP_ROOT ?= /tmp" not in makefile
    assert "build: clean" in makefile


def test_makefile_clean_uses_the_hygiene_tool():
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    assert "tools/workspace_hygiene.py prune" in makefile
    assert "hygiene-report" in makefile


def test_hygiene_tool_is_linted():
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    lint_line = next(line for line in makefile.splitlines() if 'ruff" check ' in line)
    assert "tools/workspace_hygiene.py" in lint_line


def test_check_exit_codes(tmp_path, capsys):
    (tmp_path / ".pytest_cache").mkdir()
    assert hygiene.main(["check", "--root", str(tmp_path), "--no-scratch"]) == 1
    hygiene.main(["prune", "--root", str(tmp_path), "--no-scratch"])
    assert hygiene.main(["check", "--root", str(tmp_path), "--no-scratch"]) == 0


def test_report_is_json_serialisable(tmp_path, capsys):
    import json

    (tmp_path / ".ruff_cache").mkdir()
    hygiene.main(["report", "--root", str(tmp_path), "--no-scratch", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["disposable_bytes"] >= 0
    assert isinstance(payload["disposable"], list)
