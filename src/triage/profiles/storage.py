"""Storage adapters — the local-FS / S3 artifact-IO seam (ADR-0003, cloud-profile-spec §3).

One small adapter over ``pyarrow.fs`` + ``fsspec``/``s3fs`` that dispatches local-vs-S3 by URI
scheme. It is the single storage seam: it replaces the plain-``Path`` writes in the matrix /
model builders **and** absorbs GC's ``_delete_output_file``. A matrix Parquet or model joblib
is written/read straight to ``s3://…`` with no ``/tmp`` round-trip — the credentials resolve via
the standard AWS chain (the Batch task role in cloud, the dev's env locally for testing).

``storage_root`` is a URI: ``./matrices`` locally, ``s3://$TRIAGE_S3_BUCKET/<scope>`` in cloud;
the file layout (``<uuid>.parquet`` / ``<uuid>.joblib``) is identical across schemes — only the
root differs. ``output_ref`` on ``triage.artifacts`` is the full URI (already scheme-aware), so
GC routes file-backed outputs straight back through :meth:`StorageAdapter.delete`.
"""

from __future__ import annotations

import io
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

from triage.logging import get_logger

logger = get_logger(__name__)

__all__ = [
    "LocalStorage",
    "S3Storage",
    "storage_for_root",
    "write_parquet",
    "read_parquet",
]


def _scheme(uri: str) -> str:
    """The URI scheme, treating a bare path (``./matrices``, ``/abs``) as local (``''``)."""
    return urlparse(uri).scheme


class LocalStorage:
    """Local-filesystem storage over ``pyarrow.fs.LocalFileSystem`` (scheme ``''`` / ``file``).

    Plain paths and ``file://`` URIs are local files; ``delete`` is the inherited GC
    ``Path.unlink`` branch (returns ``False`` when already absent, never an error).
    """

    def filesystem(self):
        import pyarrow.fs as pafs

        return pafs.LocalFileSystem()

    def join(self, *parts: str) -> str:
        # Local roots are plain OS paths; join with pathlib so it works on any platform.
        head, *tail = parts
        return str(Path(head).joinpath(*tail))

    def _local_path(self, uri: str) -> Path:
        parsed = urlparse(uri)
        return Path(parsed.path) if parsed.scheme == "file" else Path(uri)

    def write_bytes(self, uri: str, data: bytes) -> None:
        path = self._local_path(uri)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    @contextmanager
    def open_output(self, uri: str) -> Iterator[io.BufferedWriter]:
        path = self._local_path(uri)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as handle:
            yield handle

    @contextmanager
    def open_input(self, uri: str) -> Iterator[io.BufferedReader]:
        with open(self._local_path(uri), "rb") as handle:
            yield handle

    def delete(self, uri: str) -> bool:
        path = self._local_path(uri)
        if not path.exists():
            return False
        path.unlink()
        return True

    def exists(self, uri: str) -> bool:
        return self._local_path(uri).exists()


class S3Storage:
    """S3 storage over ``pyarrow.fs.S3FileSystem`` + ``s3fs`` (scheme ``s3``).

    Credentials resolve via the standard AWS chain — the Batch task role in cloud (ADR-0004/0005,
    no passed secrets), the dev's env (or ``moto``'s mock) locally for testing. ``region`` is
    optional; when omitted ``pyarrow``/``boto`` resolve it from the environment.
    """

    def __init__(self, region: str | None = None) -> None:
        self._region = region

    def filesystem(self):
        import pyarrow.fs as pafs

        # pyarrow strips the ``s3://`` scheme itself; paths handed to it are ``bucket/key``.
        return (
            pafs.S3FileSystem(region=self._region)
            if self._region
            else pafs.S3FileSystem()
        )

    def _fs(self):
        import s3fs

        return s3fs.S3FileSystem()

    def join(self, *parts: str) -> str:
        # S3 keys are always ``/``-joined regardless of host OS; keep the ``s3://`` scheme.
        head, *tail = parts
        parsed = urlparse(head)
        base = PurePosixPath(parsed.netloc + parsed.path)
        joined = base.joinpath(*tail)
        return f"s3://{joined}"

    def write_bytes(self, uri: str, data: bytes) -> None:
        # s3fs's open("wb") yields a binary stream; its stub types ``write`` as text-mode, so the
        # bytes write is a stub-fidelity false positive, not a real mismatch.
        with self._fs().open(uri, "wb") as handle:
            handle.write(data)  # pyright: ignore[reportArgumentType]

    @contextmanager
    def open_output(self, uri: str) -> Iterator[io.IOBase]:
        with self._fs().open(uri, "wb") as handle:
            yield handle

    @contextmanager
    def open_input(self, uri: str) -> Iterator[io.IOBase]:
        with self._fs().open(uri, "rb") as handle:
            yield handle

    def delete(self, uri: str) -> bool:
        fs = self._fs()
        if not fs.exists(uri):
            return False
        fs.rm(uri)
        return True

    def exists(self, uri: str) -> bool:
        return bool(self._fs().exists(uri))


def storage_for_root(storage_root: str, *, region: str | None = None):
    """Pick the storage adapter implied by a ``storage_root`` URI's scheme.

    Used by GC (which sees only the per-artifact ``output_ref`` URI, not a constructed Profile)
    so a file-backed output is deleted through the right adapter regardless of where the call
    originates. ``s3://`` → :class:`S3Storage`; a bare path or ``file://`` → :class:`LocalStorage`.
    """
    scheme = _scheme(storage_root)
    if scheme == "s3":
        return S3Storage(region=region)
    if scheme in ("", "file"):
        return LocalStorage()
    raise ValueError(
        f"unsupported storage scheme {scheme!r} in {storage_root!r} —"
        + " triage-pg storage handles local paths/'file://' and 's3://' only"
    )


def write_parquet(storage, uri: str, frame) -> None:
    """Write a Polars DataFrame to ``uri`` as Parquet through ``storage`` (cloud-profile-spec §3).

    The frame becomes an Arrow Table and ``pyarrow.parquet.write_table`` writes it straight onto
    the storage adapter's writable file object — local FS or, for ``s3://…``, the s3fs stream, so
    there is no ``/tmp`` round-trip and it works under ``moto`` (which intercepts botocore/s3fs,
    not pyarrow's native C++ S3 SDK). Writing to a file handle keeps one code path for both
    schemes; ``open_output`` already ``mkdir -p``s a local parent.
    """
    import pyarrow.parquet as pq

    with storage.open_output(uri) as handle:
        pq.write_table(frame.to_arrow(), handle)


def read_parquet(storage, uri: str):
    """Read a Parquet ``uri`` into a Polars DataFrame through ``storage`` (FS or s3fs stream)."""
    import polars as pl
    import pyarrow.parquet as pq

    with storage.open_input(uri) as handle:
        table = pq.read_table(handle)
    return pl.from_arrow(table)
