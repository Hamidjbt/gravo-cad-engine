"""Offline tests for the shared capability-declaring acquisition layer.

No test here touches the network: every remote fetch is served by an injected
getter returning an in-memory response double.
"""

from __future__ import annotations

import gzip
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import acquisition  # noqa: E402
from acquisition import (  # noqa: E402
    AcquisitionError,
    AcquisitionLayer,
    AcquisitionSource,
    CapabilityError,
    DownloadLimitExceeded,
    IntegrityError,
    RemoteTile,
    SourceCapabilities,
    TilePlan,
    copy_atomic,
    iter_verified_gzip,
)


class FakeResponse:
    """Minimal stand-in for a `requests` response, streaming or buffered."""

    def __init__(self, payload: bytes, status: int = 200, content_length: int | None = None):
        self._payload = payload
        self.status_code = status
        self.headers = {"Content-Length": str(content_length if content_length is not None else len(payload))}
        self.content = payload
        self.closed = False

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size: int = 8192):
        for start in range(0, len(self._payload), chunk_size):
            yield self._payload[start : start + chunk_size]

    def close(self) -> None:
        self.closed = True


def gzip_bytes(text: str) -> bytes:
    return gzip.compress(text.encode("utf-8"))


def getter_for(payload: bytes, **kwargs):
    calls: list[str] = []

    def _getter(url: str, *, stream: bool, timeout: int):
        calls.append(url)
        return FakeResponse(payload, **kwargs)

    _getter.calls = calls  # type: ignore[attr-defined]
    return _getter


CAPS = SourceCapabilities(
    source_id="test-source",
    display_name="Test Source",
    license_id="TEST-1.0",
    supports_directory_cache=True,
)


def make_layer(tmp_path: Path, caps: SourceCapabilities = CAPS, **kwargs) -> AcquisitionLayer:
    return AcquisitionLayer(caps, cache_dir=tmp_path / "source", **kwargs)


# --- capabilities ----------------------------------------------------------


def test_capability_metadata_is_machine_readable():
    payload = CAPS.to_dict()
    assert payload["source_id"] == "test-source"
    assert payload["directory_cache"] is True
    assert payload["precision_thresholds"] is False
    assert payload["accepted_suffixes"] == [".csv", ".gz"]


def test_require_raises_for_undeclared_capability():
    with pytest.raises(CapabilityError, match="does not support precision thresholds"):
        CAPS.require("supports_precision_thresholds")
    CAPS.require("supports_local_cache")  # declared -> no raise


def test_real_sources_declare_consistent_capabilities():
    import footprint_providers as fp

    capabilities = fp.provider_capabilities()
    assert set(capabilities) == {"microsoft", "google-open-buildings"}
    google = capabilities["google-open-buildings"]
    microsoft = capabilities["microsoft"]
    assert google["precision_thresholds"] is True
    assert microsoft["precision_thresholds"] is False
    assert google["directory_cache"] is True
    assert microsoft["directory_cache"] is False
    # Every source must be discoverable and locally cacheable.
    for payload in capabilities.values():
        assert payload["remote_discovery"] is True
        assert payload["local_cache"] is True
        assert payload["size_limit"] is True


# --- local inputs ----------------------------------------------------------


def test_local_file_is_accepted_with_expected_suffix(tmp_path):
    tile = tmp_path / "tile.csv.gz"
    tile.write_bytes(gzip_bytes("a,b\n1,2\n"))
    assert make_layer(tmp_path).local_paths(tile) == [tile]


def test_local_directory_is_rejected_when_not_declared(tmp_path):
    caps = SourceCapabilities(
        source_id="no-dir", display_name="No Directory", license_id="X", supports_directory_cache=False
    )
    directory = tmp_path / "cache"
    directory.mkdir()
    (directory / "tile.csv").write_text("a,b\n", encoding="utf-8")
    with pytest.raises(CapabilityError, match="does not support directory cache"):
        make_layer(tmp_path, caps).local_paths(directory)


def test_local_directory_returns_sorted_matching_files(tmp_path):
    directory = tmp_path / "cache"
    directory.mkdir()
    (directory / "b.csv").write_text("x", encoding="utf-8")
    (directory / "a.csv").write_text("x", encoding="utf-8")
    (directory / "ignore.txt").write_text("x", encoding="utf-8")
    paths = make_layer(tmp_path).local_paths(directory)
    assert [path.name for path in paths] == ["a.csv", "b.csv"]


def test_empty_local_directory_is_an_explicit_error(tmp_path):
    directory = tmp_path / "cache"
    directory.mkdir()
    with pytest.raises(FileNotFoundError, match="No .csv or .gz files found"):
        make_layer(tmp_path).local_paths(directory)


def test_missing_local_path_and_wrong_suffix_are_rejected(tmp_path):
    layer = make_layer(tmp_path)
    with pytest.raises(FileNotFoundError, match="cache does not exist"):
        layer.local_paths(tmp_path / "absent.csv")
    wrong = tmp_path / "tile.zip"
    wrong.write_text("x", encoding="utf-8")
    with pytest.raises(ValueError, match="input must be"):
        layer.local_paths(wrong)


# --- remote fetch ----------------------------------------------------------


def test_fetch_writes_and_reuses_cache(tmp_path):
    getter = getter_for(gzip_bytes("x"))
    layer = make_layer(tmp_path, getter=getter)
    tile = RemoteTile(url="https://example.test/t.csv.gz", filename="t.csv.gz")
    first = layer.fetch(tile, integrity="gzip")
    second = layer.fetch(tile, integrity="gzip")
    assert first == second == tmp_path / "source" / "t.csv.gz"
    assert len(getter.calls) == 1  # second call came from cache


def test_no_partial_file_survives_a_failed_integrity_check(tmp_path):
    getter = getter_for(b"not gzip at all")
    layer = make_layer(tmp_path, getter=getter)
    with pytest.raises(IntegrityError, match="not a readable gzip"):
        layer.fetch(RemoteTile(url="https://example.test/bad.csv.gz", filename="bad.csv.gz"), integrity="gzip")
    source = tmp_path / "source"
    assert not (source / "bad.csv.gz").exists()
    assert list(source.glob("*.part")) == []


def test_declared_size_limit_fails_before_download(tmp_path):
    getter = getter_for(gzip_bytes("x"))
    layer = make_layer(tmp_path, getter=getter, max_download_mb=1.0)
    tile = RemoteTile(url="https://example.test/big.csv.gz", filename="big.csv.gz", expected_size_mb=50.0)
    with pytest.raises(DownloadLimitExceeded, match="declared size"):
        layer.fetch(tile, integrity="gzip")


def test_content_length_limit_fails_fast(tmp_path):
    getter = getter_for(b"x" * 4096)
    layer = make_layer(tmp_path, getter=getter, max_download_mb=0.001)
    with pytest.raises(DownloadLimitExceeded, match="Content-Length"):
        layer.fetch(RemoteTile(url="https://example.test/t.csv", filename="t.csv"))


def test_streamed_bytes_are_capped_even_without_content_length(tmp_path):
    payload = b"x" * (2 * 1024 * 1024)
    getter = getter_for(payload)
    layer = make_layer(tmp_path, getter=getter, max_download_mb=0.5)
    tile = RemoteTile(url="https://example.test/t.csv", filename="t.csv")

    def lying_getter(url: str, *, stream: bool, timeout: int):
        response = FakeResponse(payload)
        response.headers = {}  # server omitted Content-Length
        return response

    layer._getter = lying_getter
    with pytest.raises(DownloadLimitExceeded, match="while streaming"):
        layer.fetch(tile)


def test_empty_body_is_an_integrity_error(tmp_path):
    getter = getter_for(b"")
    layer = make_layer(tmp_path, getter=getter)
    with pytest.raises(IntegrityError, match="empty body"):
        layer.fetch(RemoteTile(url="https://example.test/t.csv", filename="t.csv"))


def test_sha256_integrity_is_verified(tmp_path):
    payload = b"payload"
    good = RemoteTile(
        url="https://example.test/t.csv",
        filename="good.csv",
        checksum_sha256=acquisition.hashlib.sha256(payload).hexdigest(),
    )
    layer = make_layer(tmp_path, getter=getter_for(payload))
    assert layer.fetch(good, integrity="sha256").exists()

    bad = RemoteTile(url="https://example.test/b.csv", filename="bad.csv", checksum_sha256="0" * 64)
    with pytest.raises(IntegrityError, match="checksum mismatch"):
        layer.fetch(bad, integrity="sha256")


def test_sha256_mode_requires_a_checksum(tmp_path):
    layer = make_layer(tmp_path, getter=getter_for(b"payload"))
    with pytest.raises(IntegrityError, match="no checksum was supplied"):
        layer.fetch(RemoteTile(url="https://example.test/t.csv", filename="t.csv"), integrity="sha256")


def test_unknown_integrity_mode_is_rejected(tmp_path):
    layer = make_layer(tmp_path, getter=getter_for(b"x"))
    with pytest.raises(ValueError, match="unsupported integrity mode"):
        layer.fetch(RemoteTile(url="https://example.test/t.csv", filename="t.csv"), integrity="md5")


def test_transport_failure_is_normalised(tmp_path):
    def broken(url: str, *, stream: bool, timeout: int):
        raise ConnectionError("no route to host")

    layer = make_layer(tmp_path, getter=broken)
    with pytest.raises(AcquisitionError, match="could not be requested"):
        layer.fetch(RemoteTile(url="https://example.test/t.csv", filename="t.csv"))


def test_http_error_status_propagates_as_failure(tmp_path):
    layer = make_layer(tmp_path, getter=getter_for(b"x", status=503))
    with pytest.raises(RuntimeError, match="HTTP 503"):
        layer.fetch(RemoteTile(url="https://example.test/t.csv", filename="t.csv"))


def test_buffered_mode_is_used_when_streaming_is_not_declared(tmp_path):
    caps = SourceCapabilities(source_id="buffered", display_name="Buffered", license_id="X", supports_streaming=False)
    layer = make_layer(tmp_path, caps, getter=getter_for(b"csv-body"))
    path = layer.fetch(RemoteTile(url="https://example.test/t.csv", filename="t.csv"))
    assert path.read_bytes() == b"csv-body"


def test_buffered_mode_still_enforces_the_limit(tmp_path):
    caps = SourceCapabilities(source_id="buffered", display_name="Buffered", license_id="X", supports_streaming=False)
    layer = make_layer(tmp_path, caps, getter=getter_for(b"x" * 4096), max_download_mb=0.001)
    with pytest.raises(DownloadLimitExceeded, match="above the"):
        layer.fetch(RemoteTile(url="https://example.test/t.csv", filename="t.csv"))


# --- orchestration ---------------------------------------------------------


def test_resolve_fetches_plan_tiles_and_keeps_local_paths(tmp_path):
    local = tmp_path / "local.csv"
    local.write_text("x", encoding="utf-8")
    getter = getter_for(gzip_bytes("y"))
    layer = make_layer(tmp_path, getter=getter)
    plan = TilePlan(
        tiles=[RemoteTile(url="https://example.test/a.csv.gz", filename="a.csv.gz")],
        local_paths=[local],
        description="mixed plan",
    )
    paths = layer.resolve(plan, integrity="gzip")
    assert paths[0] == local
    assert paths[1].name == "a.csv.gz"
    assert layer.describe(plan) == "mixed plan"


def test_describe_falls_back_to_tile_labels(tmp_path):
    layer = make_layer(tmp_path)
    plan = TilePlan(tiles=[RemoteTile(url="u", filename="f.csv", label="quadkey 123")])
    assert "quadkey 123" in layer.describe(plan)


def test_zero_download_limit_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="greater than zero"):
        AcquisitionLayer(CAPS, cache_dir=tmp_path, max_download_mb=0)


def test_cached_ignores_empty_files(tmp_path):
    layer = make_layer(tmp_path)
    layer.cache_path("empty.csv").write_bytes(b"")
    assert layer.cached("empty.csv") is None


# --- source abstraction ----------------------------------------------------


class StubSource(AcquisitionSource):
    """A third provider implemented only by declaring capabilities and planning."""

    capabilities = SourceCapabilities(
        source_id="stub",
        display_name="Stub Footprints",
        license_id="STUB-1.0",
        supports_directory_cache=True,
    )

    def plan(self, *, lat, lon, radius_m, layer, local_file=None):
        return TilePlan(
            tiles=[RemoteTile(url=f"https://stub.test/{round(lat, 3)}.csv.gz", filename="stub.csv.gz")],
            description=f"stub tile for {lat},{lon} r={radius_m}",
        )


def test_new_source_needs_only_capabilities_and_a_plan(tmp_path):
    layer = make_layer(tmp_path, getter=getter_for(gzip_bytes("geometry")))
    paths, description = StubSource().acquire(lat=35.7, lon=-0.641, radius_m=100, layer=layer, integrity="gzip")
    assert [path.name for path in paths] == ["stub.csv.gz"]
    assert description.startswith("stub tile for 35.7")


def test_source_acquire_honours_local_override(tmp_path):
    local = tmp_path / "stub.csv.gz"
    local.write_bytes(gzip_bytes("geometry"))
    layer = make_layer(tmp_path, getter=getter_for(b"never used"))
    paths, description = StubSource().acquire(lat=0, lon=0, radius_m=1, layer=layer, local_file=local, integrity="gzip")
    assert paths == [local]
    assert description.startswith("local Stub Footprints cache")


# --- helpers ---------------------------------------------------------------


def test_iter_verified_gzip_detects_truncation(tmp_path):
    good = tmp_path / "good.gz"
    good.write_bytes(gzip_bytes("hello world"))
    assert b"".join(iter_verified_gzip(good)) == b"hello world"
    truncated = tmp_path / "truncated.gz"
    truncated.write_bytes(gzip_bytes("hello world")[:-6])
    with pytest.raises(IntegrityError, match="not a readable gzip"):
        list(iter_verified_gzip(truncated))


def test_copy_atomic_leaves_no_partial_file(tmp_path):
    source = tmp_path / "src.csv"
    source.write_text("data", encoding="utf-8")
    destination = tmp_path / "nested" / "dst.csv"
    assert copy_atomic(source, destination) == destination
    assert destination.read_text(encoding="utf-8") == "data"
    assert list(destination.parent.glob("*.part")) == []
