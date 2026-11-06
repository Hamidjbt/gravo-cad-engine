#!/usr/bin/env python3
"""Shared, capability-declaring acquisition layer for footprint sources.

Before this module existed, each provider re-implemented index fetch, tile
selection, download, size limits, atomic caching, and local-file handling. Three
sources meant three copies and three sets of diverging behaviour: Microsoft
buffered whole tiles in memory with no size cap, Google streamed with a cap.

This module owns the *mechanics*. Providers declare *policy* through
:class:`SourceCapabilities` and supply a :class:`TilePlan` describing what to
fetch. Consequences:

* local/cached override, cache reuse, and atomic writes behave identically
  everywhere;
* every download is size-limited and integrity-checked at the same point;
* adding a source means implementing the planning step, not the plumbing;
* unsupported operations fail with an explicit message derived from the
  declared capabilities rather than from ad-hoc branching.

The layer is deliberately dependency-light at import time so it can be exercised
offline in tests.
"""

from __future__ import annotations

import abc
import gzip
import hashlib
import shutil
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

USER_AGENT = "cad-fond-de-plan/1.0"
DEFAULT_TIMEOUT_S = 240
DEFAULT_MAX_DOWNLOAD_MB = 250.0
CHUNK_BYTES = 1024 * 1024
PARTIAL_SUFFIX = ".part"


class AcquisitionError(RuntimeError):
    """A source could not be acquired; the message is user-facing."""


class CapabilityError(AcquisitionError):
    """The requested operation is not supported by this source's capabilities."""


class DownloadLimitExceeded(AcquisitionError):
    """A remote object exceeded the configured per-object download limit."""


class IntegrityError(AcquisitionError):
    """A downloaded object failed its post-download integrity check."""


@dataclass(frozen=True)
class SourceCapabilities:
    """Declared, machine-readable capabilities of one footprint source.

    Everything the acquisition layer or a caller may branch on lives here, so
    behaviour differences are explicit and testable rather than implicit in code
    paths.
    """

    source_id: str
    display_name: str
    license_id: str
    default_min_confidence: float | None = None
    supports_remote_discovery: bool = True
    supports_local_cache: bool = True
    supports_directory_cache: bool = False
    supports_size_limit: bool = True
    supports_confidence_scores: bool = True
    supports_precision_thresholds: bool = False
    supports_streaming: bool = True
    accepted_suffixes: tuple[str, ...] = (".csv", ".gz")
    notes: tuple[str, ...] = ()

    def require(self, capability: str) -> None:
        """Raise :class:`CapabilityError` when ``capability`` is not declared."""
        if not getattr(self, capability, False):
            raise CapabilityError(
                f"{self.display_name} does not support {capability.replace('supports_', '').replace('_', ' ')}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "display_name": self.display_name,
            "license_id": self.license_id,
            "default_min_confidence": self.default_min_confidence,
            "remote_discovery": self.supports_remote_discovery,
            "local_cache": self.supports_local_cache,
            "directory_cache": self.supports_directory_cache,
            "size_limit": self.supports_size_limit,
            "confidence_scores": self.supports_confidence_scores,
            "precision_thresholds": self.supports_precision_thresholds,
            "accepted_suffixes": list(self.accepted_suffixes),
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class RemoteTile:
    """One remote object to fetch, with its size limit and integrity expectation."""

    url: str
    filename: str
    expected_size_mb: float | None = None
    checksum_sha256: str | None = None
    label: str = ""

    def describe(self) -> str:
        return self.label or self.filename


@dataclass
class TilePlan:
    """What to acquire for a request.

    ``tiles`` is empty when every required object is already resolved (for
    example a full local cache), which keeps callers uniform.
    """

    tiles: list[RemoteTile] = field(default_factory=list)
    local_paths: list[Path] = field(default_factory=list)
    description: str = ""
    skipped: list[str] = field(default_factory=list)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _default_response(url: str, *, stream: bool, timeout: int):
    import requests

    return requests.get(url, timeout=timeout, headers={"User-Agent": USER_AGENT}, stream=stream)


def _copy_chunks(source, destination: Path, *, max_bytes: int | None, expected_mb: float | None) -> int:
    """Stream ``source`` to ``destination``, enforcing the byte ceiling as we go.

    The ceiling is checked against both the declared size (fail fast, before any
    bytes land) and the running byte count (fail safe, if the server lied or sent
    chunked encoding with no Content-Length).
    """
    if max_bytes is not None and expected_mb is not None and expected_mb > 0:
        declared = int(expected_mb * 1024 * 1024)
        if declared > max_bytes:
            raise DownloadLimitExceeded(
                f"declared size {expected_mb:.1f} MB exceeds the {max_bytes / 1024 / 1024:.1f} MB limit"
            )
    header_size = getattr(source, "headers", {}).get("Content-Length") if hasattr(source, "headers") else None
    if header_size and max_bytes is not None and int(header_size) > max_bytes:
        raise DownloadLimitExceeded(
            f"Content-Length {int(header_size) / 1024 / 1024:.1f} MB exceeds the {max_bytes / 1024 / 1024:.1f} MB limit"
        )
    received = 0
    with destination.open("wb") as stream:
        for chunk in source.iter_content(chunk_size=CHUNK_BYTES):
            if not chunk:
                continue
            received += len(chunk)
            if max_bytes is not None and received > max_bytes:
                raise DownloadLimitExceeded(
                    f"download exceeded the {max_bytes / 1024 / 1024:.1f} MB limit while streaming"
                )
            stream.write(chunk)
    return received


class AcquisitionLayer:
    """Mechanics shared by every footprint source.

    Providers call :meth:`resolve` with a plan; they do not touch the network,
    temp files, or cache semantics themselves.
    """

    def __init__(
        self,
        capabilities: SourceCapabilities,
        *,
        cache_dir: Path,
        max_download_mb: float = DEFAULT_MAX_DOWNLOAD_MB,
        timeout_s: int = DEFAULT_TIMEOUT_S,
        getter=None,
    ) -> None:
        if max_download_mb <= 0:
            raise ValueError("max_download_mb must be greater than zero")
        self.capabilities = capabilities
        self.cache_dir = Path(cache_dir)
        self.max_download_mb = float(max_download_mb)
        self.timeout_s = int(timeout_s)
        self._getter = getter or _default_response

    # -- local inputs -------------------------------------------------------

    def local_paths(self, local: Path) -> list[Path]:
        """Resolve an explicit local file or directory, validating suffixes."""
        self.capabilities.require("supports_local_cache")
        local = Path(local).expanduser()
        if local.is_dir():
            self.capabilities.require("supports_directory_cache")
            suffixes = self.capabilities.accepted_suffixes
            found = sorted(
                path
                for path in local.iterdir()
                if path.is_file() and any(str(path).lower().endswith(sfx) for sfx in suffixes)
            )
            if not found:
                raise FileNotFoundError(
                    f"No {' or '.join(suffixes)} files found in {local} for {self.capabilities.display_name}"
                )
            return found
        if not local.is_file():
            raise FileNotFoundError(f"{self.capabilities.display_name} cache does not exist: {local}")
        if not any(str(local).lower().endswith(sfx) for sfx in self.capabilities.accepted_suffixes):
            raise ValueError(
                f"{self.capabilities.display_name} input must be {' or '.join(self.capabilities.accepted_suffixes)}"
            )
        return [local]

    # -- cache --------------------------------------------------------------

    def cache_path(self, filename: str) -> Path:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        return self.cache_dir / filename

    def cached(self, filename: str) -> Path | None:
        candidate = self.cache_dir / filename
        return candidate if candidate.is_file() and candidate.stat().st_size > 0 else None

    # -- remote -------------------------------------------------------------

    def fetch(
        self,
        tile: RemoteTile,
        *,
        integrity: str = "none",
        max_download_mb: float | None = None,
    ) -> Path:
        """Download one tile into the cache atomically and return its path.

        ``integrity`` is ``"gzip"`` to require a readable gzip stream, ``"sha256"``
        to require the declared checksum, or ``"none"`` to accept any non-empty
        body. Partial writes never become visible: bytes land in a ``.part`` file
        that is renamed only after every check passes.
        """
        self.capabilities.require("supports_remote_discovery")
        limit_mb = self.max_download_mb if max_download_mb is None else float(max_download_mb)
        if limit_mb <= 0:
            raise ValueError("max_download_mb must be greater than zero")
        destination = self.cache_path(tile.filename)
        if destination.is_file() and destination.stat().st_size > 0:
            return destination

        max_bytes = int(limit_mb * 1024 * 1024) if self.capabilities.supports_size_limit else None
        streaming = self.capabilities.supports_streaming
        try:
            response = self._getter(tile.url, stream=streaming, timeout=self.timeout_s)
        except Exception as exc:  # noqa: BLE001 - normalise transport failures
            raise AcquisitionError(f"{tile.describe()} could not be requested: {type(exc).__name__}: {exc}") from exc

        fd, temp_name = tempfile.mkstemp(prefix=destination.name + ".", suffix=PARTIAL_SUFFIX, dir=destination.parent)
        import os

        os.close(fd)
        temp_path = Path(temp_name)
        try:
            raise_for_status = getattr(response, "raise_for_status", None)
            if callable(raise_for_status):
                raise_for_status()
            if streaming and hasattr(response, "iter_content"):
                try:
                    received = _copy_chunks(response, temp_path, max_bytes=max_bytes, expected_mb=tile.expected_size_mb)
                finally:
                    close = getattr(response, "close", None)
                    if callable(close):
                        close()
            else:
                payload = getattr(response, "content", b"") or b""
                if max_bytes is not None and len(payload) > max_bytes:
                    raise DownloadLimitExceeded(
                        f"{tile.describe()} is {len(payload) / 1024 / 1024:.1f} MB, above the {limit_mb:.1f} MB limit"
                    )
                received = len(payload)
                temp_path.write_bytes(payload)
            if received == 0:
                raise IntegrityError(f"{tile.describe()} produced an empty body")

            if integrity == "gzip":
                try:
                    with gzip.open(temp_path, "rb") as probe:
                        probe.read(1024)
                except OSError as exc:
                    raise IntegrityError(f"{tile.describe()} is not a readable gzip stream: {exc}") from exc
            elif integrity == "sha256":
                if not tile.checksum_sha256:
                    raise IntegrityError(f"{tile.describe()} declared sha256 integrity but no checksum was supplied")
                actual = sha256_file(temp_path)
                if actual.lower() != tile.checksum_sha256.lower():
                    raise IntegrityError(f"{tile.describe()} checksum mismatch (expected {tile.checksum_sha256})")
            elif integrity != "none":
                raise ValueError(f"unsupported integrity mode: {integrity}")

            temp_path.replace(destination)
            return destination
        finally:
            if temp_path.exists():
                temp_path.unlink(missing_ok=True)

    def fetch_index(self, tile: RemoteTile, *, integrity: str = "none") -> Path:
        """Fetch a discovery index (tile catalogue) into the cache."""
        return self.fetch(tile, integrity=integrity)

    # -- orchestration ------------------------------------------------------

    def resolve(self, plan: TilePlan, *, integrity: str = "none") -> list[Path]:
        """Return cached paths for ``plan``, fetching whatever is missing."""
        paths = list(plan.local_paths)
        for tile in plan.tiles:
            paths.append(self.fetch(tile, integrity=integrity))
        return paths

    def describe(self, plan: TilePlan) -> str:
        if plan.description:
            return plan.description
        labels = [tile.describe() for tile in plan.tiles] + [path.name for path in plan.local_paths]
        return f"{self.capabilities.display_name}: " + (", ".join(labels) if labels else "nothing to acquire")


class AcquisitionSource(abc.ABC):
    """A footprint source: declared capabilities plus a planning step."""

    capabilities: SourceCapabilities

    @abc.abstractmethod
    def plan(
        self,
        *,
        lat: float,
        lon: float,
        radius_m: float,
        layer: AcquisitionLayer,
        local_file: Path | None = None,
    ) -> TilePlan:
        """Describe what to acquire. Must not perform I/O beyond cache probes."""

    def acquire(
        self,
        *,
        lat: float,
        lon: float,
        radius_m: float,
        layer: AcquisitionLayer,
        local_file: Path | None = None,
        integrity: str = "none",
    ) -> tuple[list[Path], str]:
        """Plan, then resolve. The single entry point providers should expose."""
        if local_file is not None:
            paths = layer.local_paths(local_file)
            return paths, f"local {self.capabilities.display_name} cache: " + ", ".join(p.name for p in paths)
        plan = self.plan(lat=lat, lon=lon, radius_m=radius_m, layer=layer)
        paths = layer.resolve(plan, integrity=integrity)
        return paths, layer.describe(plan)


def iter_verified_gzip(path: Path) -> Iterator[bytes]:
    """Yield decompressed chunks, surfacing truncation as :class:`IntegrityError`."""
    try:
        with gzip.open(path, "rb") as stream:
            while True:
                chunk = stream.read(CHUNK_BYTES)
                if not chunk:
                    return
                yield chunk
    except (OSError, EOFError) as exc:
        # gzip raises EOFError (not OSError) for a truncated stream.
        raise IntegrityError(f"{path.name} is not a readable gzip stream: {exc}") from exc


def copy_atomic(source: Path, destination: Path) -> Path:
    """Copy ``source`` to ``destination`` without exposing a partial file."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=destination.name + ".", suffix=PARTIAL_SUFFIX, dir=destination.parent)
    import os

    os.close(fd)
    temp_path = Path(temp_name)
    try:
        shutil.copyfile(source, temp_path)
        temp_path.replace(destination)
    finally:
        temp_path.unlink(missing_ok=True)
    return destination
