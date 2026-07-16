"""The three adapter Protocols (cloud-profile-spec §1).

Structural typing — every concrete adapter (local + cloud) satisfies these by shape, so they
are trivially stubbable in tests. Kept in their own module so both the concrete adapters and the
:class:`~triage.profiles.Profile` value object can depend on them without an import cycle.
"""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import AbstractContextManager
from typing import Any, Protocol, runtime_checkable

from triage.util.db import DictRowPool

__all__ = ["AuthAdapter", "StorageAdapter", "ExecutionAdapter"]


@runtime_checkable
class AuthAdapter(Protocol):
    """Produces the project ``DictRowPool`` (static password locally, RDS IAM in cloud)."""

    def open_pool(self, *, min_size: int = 1, max_size: int = 10) -> DictRowPool: ...


@runtime_checkable
class StorageAdapter(Protocol):
    """Read/write/delete artifact bytes by URI, dispatching local-FS vs S3 by scheme."""

    def join(self, *parts: str) -> str: ...

    def write_bytes(self, uri: str, data: bytes) -> None: ...

    def open_output(self, uri: str) -> AbstractContextManager[Any]: ...

    def open_input(self, uri: str) -> AbstractContextManager[Any]: ...

    def filesystem(self) -> Any: ...

    def delete(self, uri: str) -> bool: ...

    def exists(self, uri: str) -> bool: ...


@runtime_checkable
class ExecutionAdapter(Protocol):
    """In-process run vs Batch submit, wrapped around the headless core."""

    def run(
        self,
        pool: DictRowPool,
        experiment_config: Mapping[str, Any],
        *,
        storage: StorageAdapter,
        storage_root: str,
        **run_kwargs: Any,
    ) -> Any: ...
