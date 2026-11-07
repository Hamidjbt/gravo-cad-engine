"""Contract tests for the artifact egress pipeline (v1.10.0).

These tests pin the properties that make heavy rendering safe inside a
quota-monitored workspace:

* nothing heavy is written to the monitored tree;
* the scratch buffer is purged on success *and* on exception;
* every flushed artifact carries a SHA256 and a verified remote byte count;
* the receipt is lightweight, immutable, and never overwritten;
* chunked streaming emits the right offsets/commands for a real Drive session.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from tools.egress_manager import (
    CHUNK_SIZE,
    ArtifactEgress,
    ArtifactRecord,
    EgressError,
    GoogleDriveSink,
    LocalDirectorySink,
    NullSink,
    ResumableSessionSink,
    chunk_plan,
    egress_report_block,
    guess_mime,
    monitored_root_from_env,
    resolve_sink,
    sha256_file,
    workspace_footprint,
)


def _payload(path: Path, size: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\xa5" * size)
    return path


# -- pure helpers ---------------------------------------------------------


def test_chunk_plan_splits_exactly_once_the_final_chunk_may_be_short():
    assert chunk_plan(0) == []
    assert chunk_plan(CHUNK_SIZE) == [CHUNK_SIZE]
    plan = chunk_plan(2 * CHUNK_SIZE + 7)
    assert plan == [CHUNK_SIZE, CHUNK_SIZE, 7]
    assert sum(plan) == 2 * CHUNK_SIZE + 7


def test_chunk_plan_never_exceeds_granularity():
    for total in (1, CHUNK_SIZE - 1, CHUNK_SIZE + 1, 5 * CHUNK_SIZE):
        assert all(length <= CHUNK_SIZE for length in chunk_plan(total))


def test_sha256_streams_and_matches_hashlib_truth():
    import hashlib

    path = _payload(Path("/var/tmp/egress-test-sha.bin"), 4096)
    try:
        assert sha256_file(path) == hashlib.sha256(path.read_bytes()).hexdigest()
    finally:
        path.unlink()


def test_guess_mime_covers_engine_outputs():
    assert guess_mime("a.png") == "image/png"
    assert guess_mime("a.dxf") == "application/dxf"
    assert guess_mime("x.bundle") == "application/octet-stream"
    assert guess_mime("weird.zzz") == "application/octet-stream"


def test_workspace_footprint_ignores_volatile_caches(tmp_path):
    (tmp_path / "keep.bin").write_bytes(b"x" * 100)
    cache = tmp_path / ".cache"
    cache.mkdir()
    (cache / "big.bin").write_bytes(b"x" * 5000)
    assert workspace_footprint(tmp_path) == 100


# -- sink selection -------------------------------------------------------


def test_resolve_sink_rejects_gdrive_without_transport():
    with pytest.raises(EgressError, match="resumable session URIs"):
        resolve_sink("gdrive")


def test_resolve_sink_rejects_unknown_destination():
    with pytest.raises(EgressError, match="unknown egress destination"):
        resolve_sink("s3")


def test_resolve_sink_prefers_sessions_when_supplied():
    sink = resolve_sink("gdrive", sessions={"a.png": "https://example.test/session"})
    assert isinstance(sink, ResumableSessionSink)


def test_resolve_sink_builds_drive_sink_from_service():
    sink = resolve_sink("gdrive", service=object(), folder_id="folder-1")
    assert isinstance(sink, GoogleDriveSink)


# -- buffer placement -----------------------------------------------------


def test_buffer_is_created_under_var_tmp_and_purged(tmp_path):
    with ArtifactEgress(destination="null", run_id="run-buffer-001", receipt_dir=tmp_path) as egress:
        buffer = egress.buffer
        assert str(buffer).startswith("/var/tmp/gravo_egress_buffer/")
        assert buffer.is_dir()
        (buffer / "a.txt").write_text("hello", encoding="utf-8")
        assert buffer.exists()
    assert not buffer.exists()


def test_buffer_inside_monitored_tree_is_refused(tmp_path, monkeypatch):
    """A buffer placed inside the monitored tree must be rejected.

    The monitored tree is redirected with GRAVO_WORKSPACE to a temporary directory
    rather than naming any host path: a CI runner's home is not a writable tree the
    job may create, and ``mkdir(parents=True)`` against a missing one raises
    PermissionError instead of reporting the real problem.
    See test_guard_runs_before_the_buffer_is_created.
    """
    monkeypatch.setenv("GRAVO_WORKSPACE", str(tmp_path))
    egress = ArtifactEgress(
        destination="null",
        run_id="run-buffer-002",
        buffer_root=tmp_path / ".forbidden-egress",
        receipt_dir=tmp_path,
    )
    with pytest.raises(EgressError, match="monitored tree"):
        egress.__enter__()


def test_guard_runs_before_the_buffer_is_created(tmp_path, monkeypatch):
    """The placement guard must run before any directory is made.

    Regression: ``mkdir(parents=True, exist_ok=False)`` used to run first, so a
    buffer inside a *non-existent* monitored root tried to create that root. On a
    GitHub runner this surfaced as ``PermissionError: [Errno 13] Permission denied``
    on the monitored path instead of the EgressError the contract promises — passing
    on a developer sandbox, failing in CI.
    """
    absent_root = tmp_path / "no-such-workspace"
    monkeypatch.setenv("GRAVO_WORKSPACE", str(absent_root))
    egress = ArtifactEgress(
        destination="null",
        run_id="run-buffer-003",
        buffer_root=absent_root / "forbidden-egress",
        receipt_dir=tmp_path,
    )
    with pytest.raises(EgressError, match="monitored tree"):
        egress.__enter__()
    # Nothing was created: neither the buffer nor the monitored root.
    assert not absent_root.exists()


def test_monitored_root_is_taken_from_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("GRAVO_WORKSPACE", str(tmp_path / "ws"))
    assert monitored_root_from_env() == tmp_path / "ws"


def test_footprint_of_an_absent_root_is_zero_not_an_error(tmp_path):
    """Accounting a tree that does not exist contributes zero, never raises."""
    assert workspace_footprint(tmp_path / "definitely-absent") == 0


def test_receipt_records_the_redirected_monitored_root(tmp_path, monkeypatch):
    """Footprint accounting and the receipt must follow the redirect."""
    monkeypatch.setenv("GRAVO_WORKSPACE", str(tmp_path))
    with ArtifactEgress(destination="null", run_id="run-raised-002") as egress:
        egress.register(_payload(egress.buffer / "sheet.png", 16))
    receipt = json.loads((tmp_path / "run-raised-002_receipt.json").read_text(encoding="utf-8"))
    assert receipt["workspace"]["monitored_root"] == str(tmp_path)
    assert receipt["status"] == "succeeded"


def test_artifact_path_cannot_escape_the_buffer(tmp_path):
    with ArtifactEgress(destination="null", run_id="run-escape-001", receipt_dir=tmp_path) as egress:
        with pytest.raises(EgressError, match="escapes"):
            egress.path_for("../outside.png")


# -- registration rules ---------------------------------------------------


def test_registering_a_file_outside_the_buffer_is_refused(tmp_path):
    outside = _payload(tmp_path / "outside.png", 8)
    with ArtifactEgress(destination="null", run_id="run-reg-001", receipt_dir=tmp_path) as egress:
        with pytest.raises(EgressError, match="must be written inside the buffer"):
            egress.register(outside)


def test_registering_a_missing_artifact_is_refused(tmp_path):
    with ArtifactEgress(destination="null", run_id="run-reg-002", receipt_dir=tmp_path) as egress:
        with pytest.raises(EgressError, match="missing artifact"):
            egress.register(egress.buffer / "ghost.png")


def test_buffer_limit_is_enforced(tmp_path):
    with ArtifactEgress(
        destination="null",
        run_id="run-limit-001",
        receipt_dir=tmp_path,
        buffer_limit_bytes=1024,
    ) as egress:
        _payload(egress.buffer / "big.bin", 2048)
        with pytest.raises(EgressError, match="exceeded"):
            egress.register(egress.buffer / "big.bin")


def test_register_tree_names_files_relative_to_their_root(tmp_path):
    with ArtifactEgress(destination="null", run_id="run-tree-001", receipt_dir=tmp_path) as egress:
        out = egress.output_dir("final")
        _payload(out / "sheet.png", 16)
        _payload(out / "source" / "tile.csv.gz", 16)
        egress.register_tree(out, prefix="final/")
        egress.flush()
        names = {record.logical_name for record in egress.records}
    assert names == {"final/sheet.png", "final/source/tile.csv.gz"}


# -- success path ---------------------------------------------------------


def test_success_flushes_hashes_and_pulses_a_receipt(tmp_path):
    sink_dir = Path("/var/tmp/egress-test-sink")
    with ArtifactEgress(
        destination="local",
        run_id="run-success-001",
        receipt_dir=tmp_path,
        sink=LocalDirectorySink(sink_dir),
    ) as egress:
        out = egress.output_dir()
        heavy = _payload(out / "a0-sheet.png", 3 * CHUNK_SIZE + 11)
        egress.register(heavy)
        payload = egress.buffer
    receipt = json.loads((tmp_path / "run-success-001_receipt.json").read_text(encoding="utf-8"))
    assert receipt["status"] == "succeeded"
    assert receipt["buffer"]["purged"] is True
    assert receipt["totals"]["artifacts"] == 1
    assert receipt["artifacts"][0]["bytes"] == 3 * CHUNK_SIZE + 11
    assert receipt["artifacts"][0]["chunks"] == 4
    assert receipt["artifacts"][0]["verified"] is True
    assert len(receipt["artifacts"][0]["sha256"]) == 64
    assert not payload.exists()
    assert (sink_dir / "a0-sheet.png").stat().st_size == 3 * CHUNK_SIZE + 11


def test_receipt_is_read_only_and_immutable(tmp_path):
    run_id = "run-immutable-001"
    with ArtifactEgress(destination="null", run_id=run_id, receipt_dir=tmp_path) as egress:
        egress.register(_payload(egress.buffer / "a.png", 4))
    receipt = tmp_path / f"{run_id}_receipt.json"
    assert stat.S_IMODE(receipt.stat().st_mode) == 0o444
    with pytest.raises(EgressError, match="immutable receipt"):
        with ArtifactEgress(destination="null", run_id=run_id, receipt_dir=tmp_path) as egress:
            egress.register(_payload(egress.buffer / "b.png", 4))


def test_receipt_records_workspace_delta_not_artifact_bytes(tmp_path):
    with ArtifactEgress(destination="null", run_id="run-delta-001", receipt_dir=tmp_path) as egress:
        egress.register(_payload(egress.buffer / "a.png", 1024 * 1024))
    receipt = json.loads((tmp_path / "run-delta-001_receipt.json").read_text(encoding="utf-8"))
    assert receipt["totals"]["bytes_egressed"] == 1024 * 1024
    assert abs(receipt["workspace"]["delta_bytes"]) < 1024 * 1024


# -- failure path ---------------------------------------------------------


def test_exception_still_purges_the_buffer_and_writes_a_failed_receipt(tmp_path):
    buffer_holder = {}
    with pytest.raises(RuntimeError, match="boom"):
        with ArtifactEgress(destination="null", run_id="run-fail-001", receipt_dir=tmp_path) as egress:
            buffer_holder["path"] = egress.buffer
            _payload(egress.buffer / "partial.png", 4096)
            raise RuntimeError("boom")
    receipt = json.loads((tmp_path / "run-fail-001_receipt.json").read_text(encoding="utf-8"))
    assert receipt["status"] == "failed"
    assert receipt["error"].startswith("RuntimeError: boom")
    assert receipt["buffer"]["purged"] is True
    assert not buffer_holder["path"].exists()


def test_unverified_remote_is_an_error_not_a_silent_pass(tmp_path):
    class LyingSink(NullSink):
        name = "lying"

        def put(self, path, logical_name, content_type):
            record = super().put(path, logical_name, content_type)
            return ArtifactRecord(
                logical_name=record.logical_name,
                bytes=record.bytes,
                sha256=record.sha256,
                content_type=record.content_type,
                remote_uri="null://x",
                remote_bytes=record.bytes - 1,
            )

    with pytest.raises(EgressError, match="remote size mismatch"):
        with ArtifactEgress(destination="null", run_id="run-lie-001", receipt_dir=tmp_path, sink=LyingSink()) as egress:
            egress.register(_payload(egress.buffer / "a.png", 32))


def test_declared_gdrive_destination_without_sessions_fails_closed(tmp_path):
    """A sink that cannot be built must fail before any work, leaving no scratch space."""
    run_id = "run-nocred-001"
    buffer = Path("/var/tmp/gravo_egress_buffer") / run_id
    with pytest.raises(EgressError, match="resumable session URIs"):
        with ArtifactEgress(destination="gdrive", run_id=run_id, receipt_dir=tmp_path):
            pass  # pragma: no cover - enter() raises
    # __enter__ raises, so __exit__ never runs; the buffer must still be gone.
    assert not buffer.exists()


# -- chunked resumable transport -----------------------------------------


class _RecordingClient:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def put(self, url, content, headers):
        class Response:
            status_code = 200
            headers = {"X-Goog-Upload-Status": "active"}
            text = ""

            def json(self):
                return {"id": "srv-id-9", "name": "chunks.bin"}

        # The last chunk announces completion.
        if headers["X-Goog-Upload-Command"].endswith("finalize"):
            Response.headers = {"X-Goog-Upload-Status": "final"}
        self.calls.append({"url": url, "len": len(content), "headers": dict(headers)})
        return Response()


def test_resumable_sink_streams_ordered_chunks_with_drive_headers(tmp_path, monkeypatch):
    import httpx

    client = _RecordingClient()
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: client)

    path = _payload(Path("/var/tmp/egress-test-chunks.bin"), 2 * CHUNK_SIZE + 5)
    try:
        sink = ResumableSessionSink(
            {"chunks.bin": "https://upload.test/session"},
            declared_sizes={"chunks.bin": 2 * CHUNK_SIZE + 5},
        )
        record = sink.put(path, "chunks.bin", "application/octet-stream")
    finally:
        path.unlink()

    assert record.chunks == 3
    assert record.verified is True
    assert record.remote_uri == "https://drive.google.com/file/d/srv-id-9/view"
    assert [c["len"] for c in client.calls] == [CHUNK_SIZE, CHUNK_SIZE, 5]
    assert [c["headers"]["X-Goog-Upload-Offset"] for c in client.calls] == ["0", str(CHUNK_SIZE), str(2 * CHUNK_SIZE)]
    assert [c["headers"]["X-Goog-Upload-Command"] for c in client.calls] == [
        "upload",
        "upload",
        "upload, finalize",
    ]
    total = str(2 * CHUNK_SIZE + 5)
    assert all(c["headers"]["X-Goog-Upload-Total-Size"] == total for c in client.calls)


def test_resumable_sink_requires_a_declared_total_for_multichunk(tmp_path):
    """Drive rejects short non-final chunks, so a multi-chunk session must declare its total.

    Verified against the live endpoint: an undeclared 900 kB upload failed with
    HTTP 400 "not a multiple of the 262144 byte chunk granularity".
    """
    sink = ResumableSessionSink({"a.bin": "https://upload.test/session"})
    path = _payload(tmp_path / "a.bin", 3 * CHUNK_SIZE)
    with pytest.raises(EgressError, match="must declare its total size"):
        sink.put(path, "a.bin", "application/octet-stream")


def test_resumable_sink_accepts_a_single_short_chunk_without_a_total(tmp_path, monkeypatch):
    """One sub-granularity chunk finalizes fine; the granularity rule only binds mid-stream."""
    import httpx

    client = _RecordingClient()
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: client)
    sink = ResumableSessionSink({"small.bin": "https://upload.test/session"})
    record = sink.put(_payload(tmp_path / "small.bin", 4096), "small.bin", "application/octet-stream")
    assert record.chunks == 1
    assert record.verified is True
    assert client.calls[0]["headers"]["X-Goog-Upload-Command"] == "upload, finalize"


def test_resumable_sink_refuses_a_size_that_contradicts_the_session(tmp_path):
    sink = ResumableSessionSink({"a.bin": "https://upload.test/session"}, declared_sizes={"a.bin": 10})
    path = _payload(tmp_path / "a.bin", 64)
    with pytest.raises(EgressError, match="session declared 10 bytes"):
        sink.put(path, "a.bin", "application/octet-stream")


def test_resumable_sink_requires_a_session_for_each_artifact(tmp_path):
    sink = ResumableSessionSink({})
    path = _payload(tmp_path / "a.png", 4)
    with pytest.raises(EgressError, match="no resumable session URI"):
        sink.put(path, "a.png", "image/png")


def test_resumable_sink_reports_rejected_chunk(tmp_path, monkeypatch):
    import httpx

    class Rejecting(_RecordingClient):
        def put(self, url, content, headers):
            class Response:
                status_code = 503
                headers = {"X-Goog-Upload-Status": "active"}
                text = "backend unavailable"

            return Response()

    monkeypatch.setattr(httpx, "Client", lambda **kwargs: Rejecting())
    path = _payload(tmp_path / "a.png", 1024)
    sink = ResumableSessionSink({"a.png": "https://upload.test/session"})
    with pytest.raises(EgressError, match="rejected"):
        sink.put(path, "a.png", "image/png")


# -- integration ----------------------------------------------------------


def test_full_lifecycle_leaves_only_the_receipt_in_monitored_tree(tmp_path):
    window = Path("/var/tmp/egress-window-test")
    if window.exists():
        import shutil as _shutil

        _shutil.rmtree(window)
    window.mkdir()
    before = workspace_footprint(window)
    with ArtifactEgress(
        destination="local",
        run_id="run-lifecycle-001",
        receipt_dir=window,
        sink=LocalDirectorySink(Path("/var/tmp/egress-lifecycle-sink")),
    ) as egress:
        for index in range(5):
            egress.register(_payload(egress.output_dir() / f"sheet{index}.png", 64 * 1024))
    after = workspace_footprint(window)
    produced = sorted(p.name for p in window.iterdir())
    assert produced == ["run-lifecycle-001_receipt.json"]
    # The only footprint delta is the receipt itself, and it is kilobytes not megabytes.
    assert 0 <= after - before < 16 * 1024


def test_egress_report_block_names_uris_for_findings(tmp_path):
    with ArtifactEgress(destination="null", run_id="run-report-001", receipt_dir=tmp_path) as egress:
        egress.register(_payload(egress.buffer / "sheet.png", 8))
        egress.flush()
        block = egress_report_block(egress)
    assert block["run_id"] == "run-report-001"
    assert block["destination"] == "null"
    assert block["artifacts"]["sheet.png"] == "null://sheet.png"
