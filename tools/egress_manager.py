"""Artifact egress: decouple heavy artifact generation from the monitored workspace.

The monitored persistent tree (``/home/user``) is a quota-constrained resource. This
module makes artifact *generation* independent of artifact *retention*:

* every heavy output is produced inside a bounded scratch buffer rooted at
  ``/var/tmp/gravo_egress_buffer/`` — never in the monitored tree;
* on context exit the buffer is streamed to a remote sink and then purged,
  on both the success and the exception path;
* the single durable artefact left behind is ``<run_id>_receipt.json`` — a
  lightweight, immutable manifest carrying remote URIs, byte counts and SHA256
  checksums that prove the payload was secured externally.

Nothing here disables rendering. The engine keeps producing full-resolution A0/A1
sheets; only their *location* changes.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

#: Scratch space for in-flight artifacts. Deliberately outside the monitored tree.
DEFAULT_BUFFER_ROOT = Path("/var/tmp/gravo_egress_buffer")

#: The quota-constrained persistent tree. Artifacts must never land here.
MONITORED_ROOT = Path("/home/user")

#: Google's documented granularity for resumable upload; also the connector contract.
CHUNK_SIZE = 256 * 1024

#: Guard so a runaway run cannot silently exhaust the scratch device.
DEFAULT_BUFFER_LIMIT_BYTES = 8 * 1024**3

#: Directory names that are regenerable noise, excluded from footprint accounting.
_VOLATILE_DIRS = {".cache", "__pycache__", ".mypy_cache", ".ruff_cache", ".git"}

_MIME_TYPES = {
    ".svg": "image/svg+xml",
    ".dxf": "application/dxf",
    ".png": "image/png",
    ".pdf": "application/pdf",
    ".json": "application/json",
    ".csv": "text/csv",
    ".gz": "application/gzip",
    ".bundle": "application/octet-stream",
    ".osm": "application/xml",
    ".txt": "text/plain",
}


class EgressError(RuntimeError):
    """Raised when an artifact cannot be secured remotely or the invariant is broken."""


def guess_mime(name: str) -> str:
    """Return a content type for ``name`` without guessing beyond the extension."""
    return _MIME_TYPES.get(Path(name).suffix.lower(), "application/octet-stream")


def sha256_file(path: Path, block: int = 1024 * 1024) -> str:
    """Stream a file through SHA256 so hashing never loads it into memory."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(block), b""):
            digest.update(chunk)
    return digest.hexdigest()


def chunk_plan(total_bytes: int, chunk_size: int = CHUNK_SIZE) -> list[int]:
    """Return the byte length of every chunk; the final chunk may be short."""
    if total_bytes <= 0:
        return []
    full, remainder = divmod(total_bytes, chunk_size)
    plan = [chunk_size] * full
    if remainder:
        plan.append(remainder)
    return plan


def workspace_footprint(root: Path = MONITORED_ROOT) -> int:
    """Sum retained bytes under the monitored tree, ignoring regenerable caches."""
    total = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _VOLATILE_DIRS]
        for name in filenames:
            try:
                total += os.lstat(os.path.join(dirpath, name)).st_size
            except OSError:
                continue
    return total


@dataclass(frozen=True)
class ArtifactRecord:
    """One secured artifact: local proof (SHA256) plus remote address."""

    logical_name: str
    bytes: int
    sha256: str
    content_type: str
    remote_uri: str | None = None
    remote_id: str | None = None
    chunk_size: int = CHUNK_SIZE
    chunks: int = 0
    remote_bytes: int | None = None

    @property
    def verified(self) -> bool:
        """True when a remote sink confirmed it received the exact byte count."""
        return self.remote_uri is not None and self.remote_bytes == self.bytes

    def as_dict(self) -> dict[str, Any]:
        return {**asdict(self), "verified": self.verified}


class Sink(Protocol):
    """A destination that can accept an artifact body and address it afterwards."""

    name: str

    def put(self, path: Path, logical_name: str, content_type: str) -> ArtifactRecord: ...


class NullSink:
    """Records artifacts without moving them; used to prove lifecycle behaviour."""

    name = "null"

    def put(self, path: Path, logical_name: str, content_type: str) -> ArtifactRecord:
        size = path.stat().st_size
        return ArtifactRecord(
            logical_name=logical_name,
            bytes=size,
            sha256=sha256_file(path),
            content_type=content_type,
            remote_uri=f"null://{logical_name}",
            remote_id=None,
            chunks=len(chunk_plan(size)),
            remote_bytes=size,
        )


class LocalDirectorySink:
    """Copies artifacts to a directory outside the monitored tree.

    Intended for CI and offline verification of the pipeline's plumbing, not as a
    production target: it proves the flush/purge/receipt contract deterministically.
    """

    name = "local-directory"

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def put(self, path: Path, logical_name: str, content_type: str) -> ArtifactRecord:
        target = self.root / logical_name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        size = target.stat().st_size
        digest = sha256_file(target)
        return ArtifactRecord(
            logical_name=logical_name,
            bytes=size,
            sha256=digest,
            content_type=content_type,
            remote_uri=target.as_uri(),
            remote_id=None,
            chunks=len(chunk_plan(size)),
            remote_bytes=size,
        )


class GoogleDriveSink:
    """Streams artifacts to Google Drive with resumable, chunked uploads.

    Uses ``MediaFileUpload`` in resumable mode so a file is never buffered whole in
    process memory: Drive receives it in ``chunk_size`` slices and reports progress
    per slice. Credentials are supplied by the caller; this sandbox has none, which
    is why the connector-session sink below exists.

    The Drive client is imported lazily so the rest of the module stays importable
    with test-only dependencies installed.
    """

    name = "google-drive"

    def __init__(self, service: Any, folder_id: str, chunk_size: int = CHUNK_SIZE) -> None:
        self._service = service
        self._folder_id = folder_id
        self._chunk_size = chunk_size

    @classmethod
    def from_credentials(cls, credentials: Any, folder_id: str) -> GoogleDriveSink:
        """Build a sink from google-auth credentials using the Drive v3 client."""
        try:
            from googleapiclient.discovery import build  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - exercised only with the extra installed
            raise EgressError("google-api-python-client is required for the Drive sink") from exc
        service = build("drive", "v3", credentials=credentials, cache_discovery=False)
        return cls(service, folder_id)

    def put(self, path: Path, logical_name: str, content_type: str) -> ArtifactRecord:
        try:
            from googleapiclient.http import MediaFileUpload  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover
            raise EgressError("google-api-python-client is required for the Drive sink") from exc

        size = path.stat().st_size
        digest = sha256_file(path)
        media = MediaFileUpload(
            str(path),
            mimetype=content_type,
            chunksize=self._chunk_size,
            resumable=True,
        )
        request = self._service.files().create(
            body={"name": logical_name, "parents": [self._folder_id]},
            media_body=media,
            fields="id,webViewLink,size",
        )
        response = None
        chunks = 0
        while response is None:
            status, response = request.next_chunk()
            chunks += 1
            if status is not None and not status.resumable():
                raise EgressError(f"upload of {logical_name} stalled at {status.progress()}")
        remote_id = response.get("id")
        return ArtifactRecord(
            logical_name=logical_name,
            bytes=size,
            sha256=digest,
            content_type=content_type,
            remote_uri=response.get("webViewLink") or f"gdrive://{remote_id}",
            remote_id=remote_id,
            chunk_size=self._chunk_size,
            chunks=chunks,
            remote_bytes=int(response.get("size")) if response.get("size") else size,
        )


class ResumableSessionSink:
    """Streams artifacts over pre-authorized Drive resumable-upload session URIs.

    A Drive resumable session is a plain HTTP endpoint: ``PUT`` slices with
    ``X-Goog-Upload-Offset``/``-Command`` and the server accumulates them remotely.
    This sink therefore streams chunk-by-chunk straight from the scratch buffer with
    no local staging copy and no Drive client library, which is exactly what a
    credential-less execution environment needs. Session URIs are issued by the
    Drive connector (valid seven days) and mapped to logical artifact names.
    """

    name = "gdrive-resumable-session"

    def __init__(
        self,
        sessions: dict[str, str],
        chunk_size: int = CHUNK_SIZE,
        timeout: float = 300.0,
        file_ids: dict[str, str] | None = None,
        declared_sizes: dict[str, int] | None = None,
    ) -> None:
        self._sessions = dict(sessions)
        self._chunk_size = chunk_size
        self._timeout = timeout
        self._file_ids = dict(file_ids or {})
        #: Sizes announced to Drive when the session was initiated. When absent the
        #: upload must fit one chunk, because a multi-chunk stream cannot be
        #: reassembled remotely without a declared total.
        self._declared = dict(declared_sizes or {})

    def put(self, path: Path, logical_name: str, content_type: str) -> ArtifactRecord:
        session = self._sessions.get(logical_name)
        if not session:
            raise EgressError(
                f"no resumable session URI supplied for {logical_name!r}; "
                "request one from the Drive connector before flushing"
            )
        import httpx

        size = path.stat().st_size
        digest = sha256_file(path)
        plan = chunk_plan(size, self._chunk_size)
        declared = self._declared.get(logical_name)
        if declared is not None and declared != size:
            raise EgressError(
                f"{logical_name}: session declared {declared} bytes but the artifact is {size}; "
                "Drive would reject the finalize"
            )
        # A multi-chunk resumable session is only assemblable when the total is known
        # up front: the endpoint requires 256 KiB granularity and rejects a short
        # middle or trailing data chunk unless it is the announced final size. The
        # engine's growth-triggered attempts to relax this were refused by the
        # service itself (HTTP 400, "not a multiple of the 262144 byte chunk
        # granularity"), so this guard encodes the service's real contract rather
        # than an assumption about it.
        if declared is None and len(plan) > 1:
            raise EgressError(
                f"{logical_name}: {size} bytes needs {len(plan)} chunks; a resumable session "
                "must declare its total size and chunk granularity when it is created"
            )
        deferred_finalize = False
        offset = 0
        remote_id: str | None = None
        with path.open("rb") as handle, httpx.Client(timeout=self._timeout) as client:
            for index, length in enumerate(plan):
                payload = handle.read(length)
                if len(payload) != length:
                    raise EgressError(f"short read at offset {offset} for {logical_name}")
                final = index == len(plan) - 1 and not deferred_finalize
                headers = {
                    "Content-Length": str(length),
                    "Content-Type": content_type,
                    "X-Goog-Upload-Offset": str(offset),
                    "X-Goog-Upload-Command": "upload, finalize" if final else "upload",
                }
                if declared is not None:
                    headers["X-Goog-Upload-Total-Size"] = str(declared)
                response = client.put(session, content=payload, headers=headers)
                status = response.headers.get("X-Goog-Upload-Status", "")
                if response.status_code >= 400 or status not in {"active", "final"}:
                    raise EgressError(
                        f"chunk {index} of {logical_name} rejected: "
                        f"HTTP {response.status_code} status={status!r} {response.text[:300]}"
                    )
                if final:
                    # The finalize response carries the authoritative Drive file id.
                    try:
                        remote_id = response.json().get("id")
                    except Exception:  # pragma: no cover - body is JSON in practice
                        remote_id = None
                offset += length
            if deferred_finalize:
                response = client.put(
                    session,
                    content=b"",
                    headers={
                        "Content-Length": "0",
                        "Content-Type": content_type,
                        "X-Goog-Upload-Offset": str(offset),
                        "X-Goog-Upload-Command": "upload, finalize",
                    },
                )
                status = response.headers.get("X-Goog-Upload-Status", "")
                if response.status_code >= 400 or status != "final":
                    raise EgressError(
                        f"finalize of {logical_name} rejected: "
                        f"HTTP {response.status_code} status={status!r} {response.text[:300]}"
                    )
                try:
                    remote_id = response.json().get("id") or remote_id
                except Exception:  # pragma: no cover
                    pass
        if offset != size:
            raise EgressError(f"streamed {offset} of {size} bytes for {logical_name}")
        remote_id = remote_id or self._file_ids.get(logical_name)
        return ArtifactRecord(
            logical_name=logical_name,
            bytes=size,
            sha256=digest,
            content_type=content_type,
            remote_uri=f"https://drive.google.com/file/d/{remote_id}/view" if remote_id else f"gdrive://{logical_name}",
            remote_id=remote_id,
            chunk_size=self._chunk_size,
            chunks=len(plan),
            remote_bytes=size,
        )


def resolve_sink(
    destination: str,
    *,
    folder_id: str | None = None,
    sessions: dict[str, str] | None = None,
    file_ids: dict[str, str] | None = None,
    declared_sizes: dict[str, int] | None = None,
    service: Any | None = None,
    local_root: Path | None = None,
) -> Sink:
    """Construct a sink for ``destination``; each name maps to one transport."""
    if destination in {"gdrive", "gdrive_session"}:
        if sessions:
            return ResumableSessionSink(sessions, file_ids=file_ids, declared_sizes=declared_sizes)
        if service is not None and folder_id:
            return GoogleDriveSink(service, folder_id)
        raise EgressError(
            "gdrive destination requires either resumable session URIs or a Drive service object plus a folder id"
        )
    if destination == "local":
        if local_root is None:
            raise EgressError("local destination requires a directory outside the monitored tree")
        return LocalDirectorySink(local_root)
    if destination == "null":
        return NullSink()
    raise EgressError(f"unknown egress destination: {destination!r}")


@dataclass
class ArtifactEgress:
    """Context-managed owner of the scratch buffer and the egress lifecycle.

    ``with ArtifactEgress(destination='gdrive') as egress:`` creates a private buffer,
    hands it to the build pipeline as the output directory, and guarantees on exit —
    success or exception — that every registered artifact was secured remotely, that
    a receipt records the proof, and that the buffer no longer exists.
    """

    destination: str = "gdrive"
    milestone: str | None = None
    run_id: str | None = None
    buffer_root: Path = DEFAULT_BUFFER_ROOT
    receipt_dir: Path = MONITORED_ROOT
    buffer_limit_bytes: int = DEFAULT_BUFFER_LIMIT_BYTES
    sink: Sink | None = None
    sessions: dict[str, str] = field(default_factory=dict)
    file_ids: dict[str, str] = field(default_factory=dict)
    declared_sizes: dict[str, int] = field(default_factory=dict)
    folder_id: str | None = None
    service: Any | None = None
    keep_buffer_on_error: bool = False

    _buffer: Path | None = field(default=None, init=False, repr=False)
    _pending: list[tuple[str, Path]] = field(default_factory=list, init=False, repr=False)
    _records: list[ArtifactRecord] = field(default_factory=list, init=False, repr=False)
    _started: str = field(default="", init=False)
    _footprint_before: int = field(default=0, init=False)
    _status: str = field(default="initialised", init=False)
    _error: str | None = field(default=None, init=False)
    _purged: bool = field(default=False, init=False)
    _receipt_path: Path | None = field(default=None, init=False)

    # -- lifecycle ---------------------------------------------------------

    def __enter__(self) -> ArtifactEgress:
        self._started = datetime.now(UTC).isoformat(timespec="seconds")
        self.run_id = self.run_id or f"run-{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}"
        self._footprint_before = workspace_footprint(MONITORED_ROOT)
        self._buffer = self.buffer_root / self.run_id
        if self._buffer.exists():  # pragma: no cover - defensive
            shutil.rmtree(self._buffer)
        self._buffer.mkdir(parents=True, exist_ok=False)
        self._assert_outside_monitored(self._buffer)
        try:
            if self.sink is None:
                self.sink = resolve_sink(
                    self.destination,
                    folder_id=self.folder_id,
                    sessions=self.sessions or None,
                    file_ids=self.file_ids or None,
                    declared_sizes=self.declared_sizes or None,
                    service=self.service,
                )
        except Exception:
            # A sink that cannot be constructed must not leave scratch space behind;
            # __exit__ never runs when __enter__ raises.
            shutil.rmtree(self._buffer, ignore_errors=True)
            self._buffer = None
            raise
        self._status = "running"
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        try:
            if exc_type is not None:
                self._status = "failed"
                self._error = f"{exc_type.__name__}: {exc}"
            else:
                self.flush()
                self._status = "succeeded"
        except Exception as flush_error:  # pragma: no cover - defensive
            self._status = "failed"
            self._error = f"{type(flush_error).__name__}: {flush_error}"
            if exc_type is None:
                raise
        finally:
            if self._status != "succeeded" and self.keep_buffer_on_error:
                pass  # deliberate forensic retention; operator removes it manually
            else:
                self._purge()
            self._write_receipt()
            self._verify_invariant()
        return False

    # -- buffer ------------------------------------------------------------

    @property
    def buffer(self) -> Path:
        """The run's private scratch directory; valid only inside the context."""
        if self._buffer is None:
            raise EgressError("egress buffer is only available inside the context manager")
        return self._buffer

    def _assert_outside_monitored(self, path: Path) -> None:
        resolved = path.resolve()
        try:
            resolved.relative_to(MONITORED_ROOT.resolve())
        except ValueError:
            return
        raise EgressError(f"refusing to place the egress buffer inside the monitored tree: {resolved}")

    def path_for(self, name: str) -> Path:
        """Return a buffer path for ``name``, refusing any escape from the buffer."""
        candidate = (self.buffer / name).resolve()
        try:
            candidate.relative_to(self.buffer.resolve())
        except ValueError as exc:
            raise EgressError(f"artifact path escapes the egress buffer: {name!r}") from exc
        return candidate

    def output_dir(self, name: str = "artifacts") -> Path:
        """Create and return an output directory inside the buffer."""
        target = self.path_for(name)
        target.mkdir(parents=True, exist_ok=True)
        return target

    def _check_limit(self) -> None:
        used = sum(path.stat().st_size for _, path in self._pending if path.exists())
        if used > self.buffer_limit_bytes:
            raise EgressError(f"egress buffer exceeded {self.buffer_limit_bytes} bytes ({used} in flight)")

    # -- registration and flush -------------------------------------------

    def register(self, path: Path, logical_name: str | None = None) -> None:
        """Register a produced file for egress; the file must live in the buffer."""
        resolved = Path(path).resolve()
        try:
            resolved.relative_to(self.buffer.resolve())
        except ValueError as exc:
            raise EgressError(f"refusing to egress {resolved}: artifacts must be written inside the buffer") from exc
        if not resolved.is_file():
            raise EgressError(f"cannot egress missing artifact: {resolved}")
        name = logical_name or resolved.name
        if any(existing == name for existing, _ in self._pending):
            return
        self._pending.append((name, resolved))
        self._check_limit()

    def register_tree(self, directory: Path, prefix: str = "") -> None:
        """Register every regular file under ``directory`` with a relative name."""
        base = Path(directory).resolve()
        for path in sorted(base.rglob("*")):
            if path.is_file():
                rel = path.relative_to(base).as_posix()
                self.register(path, f"{prefix}{rel}" if prefix else rel)

    def flush(self) -> list[ArtifactRecord]:
        """Stream every pending artifact to the sink; verifies each byte count."""
        if self.sink is None:  # pragma: no cover - defensive
            raise EgressError("no sink configured")
        produced: list[ArtifactRecord] = []
        while self._pending:
            name, path = self._pending.pop(0)
            record = self.sink.put(path, name, guess_mime(name))
            if not record.verified:
                raise EgressError(
                    f"remote size mismatch for {name}: local {record.bytes} vs remote {record.remote_bytes}"
                )
            self._records.append(record)
            produced.append(record)
        return produced

    def uri_map(self) -> dict[str, str]:
        """Map logical artifact names to their remote URIs."""
        return {record.logical_name: record.remote_uri or "" for record in self._records}

    def resolve_uris(self, names: Iterable[str]) -> dict[str, str]:
        """Return remote URIs for known logical names, or a pending marker."""
        known = self.uri_map()
        return {name: known.get(name, "pending://not-yet-flushed") for name in names}

    @property
    def records(self) -> list[ArtifactRecord]:
        return list(self._records)

    # -- teardown ----------------------------------------------------------

    def _purge(self) -> None:
        if self._buffer is not None and self._buffer.exists():
            shutil.rmtree(self._buffer, ignore_errors=True)
        if self._buffer is not None and self._buffer.exists():  # pragma: no cover - defensive
            raise EgressError(f"egress buffer survived purge: {self._buffer}")
        self._purged = self._buffer is None or not self._buffer.exists()

    def _receipt_payload(self) -> dict[str, Any]:
        total = sum(record.bytes for record in self._records)
        unverified = [r.logical_name for r in self._records if not r.verified]
        return {
            "schema": "gravo.artifact_egress.receipt/1",
            "run_id": self.run_id,
            "milestone": self.milestone,
            "destination": self.sink.name if self.sink else self.destination,
            "status": self._status,
            "error": self._error,
            "started_at": self._started,
            "finished_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "buffer": {"root": str(self.buffer_root), "purged": self._purged},
            "artifacts": [record.as_dict() for record in self._records],
            "totals": {
                "artifacts": len(self._records),
                "bytes_egressed": total,
                "bytes_unverified": 0 if not unverified else total,
                "artifacts_unverified": unverified,
            },
            "workspace": {
                "monitored_root": str(MONITORED_ROOT),
                "bytes_before": self._footprint_before,
            },
        }

    def _write_receipt(self) -> None:
        payload = self._receipt_payload()
        payload["workspace"]["bytes_after"] = workspace_footprint(MONITORED_ROOT)
        payload["workspace"]["delta_bytes"] = payload["workspace"]["bytes_after"] - payload["workspace"]["bytes_before"]
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()
        payload["receipt_sha256"] = digest

        self.receipt_dir.mkdir(parents=True, exist_ok=True)
        target = self.receipt_dir / f"{self.run_id}_receipt.json"
        if target.exists():
            raise EgressError(f"refusing to overwrite an immutable receipt: {target}")
        target.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        os.chmod(target, 0o444)
        self._receipt_path = target

    def _verify_invariant(self) -> None:
        if self._status == "succeeded" and self._records and not all(r.verified for r in self._records):
            raise EgressError("invariant violated: artifacts were not verified remotely")
        if not self._purged:
            raise EgressError("invariant violated: egress buffer was not purged")

    @property
    def receipt_path(self) -> Path | None:
        return self._receipt_path


def egress_report_block(egress: ArtifactEgress) -> dict[str, Any]:
    """Summarise an egress run for inclusion in a build report."""
    return {
        "run_id": egress.run_id,
        "destination": egress.sink.name if egress.sink else egress.destination,
        "milestone": egress.milestone,
        "receipt": str(egress.receipt_path) if egress.receipt_path else None,
        "artifacts": {record.logical_name: record.remote_uri for record in egress.records},
    }
