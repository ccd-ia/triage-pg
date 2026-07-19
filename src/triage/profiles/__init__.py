"""The ``Profile`` value object and ``load_profile`` — the cloud-profile seam (ADR-0003).

A frozen :class:`Profile` bundles the three swappable environment adapters
(``{auth, storage, execution}``) and is the single seam the CLI constructs — the only place that
knows which environment we are in. :func:`load_profile` builds one from ``--profile`` + the
environment (cloud parameters per cloud-profile-spec §5, fail-fast on a missing var under cloud).

Where each adapter touches the flow (minimal blast radius on the tested core, spec §1):

* ``auth`` is used *before* the core: ``pool = profile.auth.open_pool()``.
* ``execution`` wraps *around* the core (in-process run vs Batch submit).
* ``storage`` threads *into* the core (the matrix/model builders + GC write/read/delete through it).
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass

from triage.logging import get_logger
from triage.profiles.auth import CloudAuth, LocalAuth
from triage.profiles.execution import (
    BatchExecution,
    InProcessExecution,
    RunHandle,
)
from triage.profiles.protocols import (
    AuthAdapter,
    ExecutionAdapter,
    StorageAdapter,
)
from triage.profiles.storage import LocalStorage, S3Storage, storage_for_root

logger = get_logger(__name__)

__all__ = [
    "Profile",
    "load_profile",
    "RunHandle",
    "AuthAdapter",
    "StorageAdapter",
    "ExecutionAdapter",
    "LocalAuth",
    "CloudAuth",
    "LocalStorage",
    "S3Storage",
    "InProcessExecution",
    "BatchExecution",
    "storage_for_root",
]


@dataclass(frozen=True)
class Profile:
    """The environment bundle: name + the three swappable adapters (cloud-profile-spec §1)."""

    name: str
    auth: AuthAdapter
    storage: StorageAdapter
    execution: ExecutionAdapter
    storage_root: str


def _require_env(var: str) -> str:
    """Fetch a required cloud env var, failing loudly with its name (CLAUDE.md error policy).

    No silent default — under ``--profile cloud`` a missing connection/storage/Batch parameter
    is a configuration error the operator must fix, not something to guess at (spec §5).
    """
    value = os.environ.get(var)
    if not value:
        raise ValueError(
            f"--profile cloud requires the environment variable {var!r} to be set"
            + " (cloud-profile-spec §5); it is missing or empty"
        )
    return value


def load_profile(
    name: str,
    *,
    dburl: str | None = None,
    storage_root: str | None = None,
    config_uri: str | None = None,
    token_provider: Callable[[], str] | None = None,
) -> Profile:
    """Build a :class:`Profile` from ``--profile`` + the environment.

    Args:
        name: ``'local'`` | ``'cloud'``.
        dburl: the resolved static-password URL (local profile only; the CLI passes
            :func:`triage.cli.resolve_db_url`'s result). Ignored under cloud (IAM, no password).
        storage_root: the artifact root URI. Local default is the CLI's ``--project-path``;
            cloud derives ``s3://$TRIAGE_S3_BUCKET`` from the env when not given.
        config_uri: the S3 URI the cloud execution adapter stages the config to and the
            container reads. Defaults to ``<storage_root>/config.json`` under cloud.
        token_provider: an injectable RDS-IAM token provider (tests pass a stub; production
            leaves it ``None`` to use the real boto3 generator). Cloud only.

    Raises:
        ValueError: an unknown profile name, or a missing required cloud env var (fail-fast).
    """
    if name == "local":
        if dburl is None:
            raise ValueError(
                "load_profile('local') requires a dburl (the resolved static-password URL)"
            )
        root = storage_root if storage_root is not None else os.getcwd()
        return Profile(
            name="local",
            auth=LocalAuth(dburl),
            storage=LocalStorage(),
            execution=InProcessExecution(),
            storage_root=root,
        )

    if name == "cloud":
        region = _require_env("AWS_REGION")
        host = _require_env("TRIAGE_RDS_HOST")
        port = int(os.environ.get("TRIAGE_RDS_PORT", "5432"))
        dbname = _require_env("TRIAGE_RDS_DB")
        user = _require_env("TRIAGE_RDS_USER")
        bucket = _require_env("TRIAGE_S3_BUCKET")

        root = storage_root if storage_root is not None else f"s3://{bucket}"
        storage = S3Storage(region=region)
        cfg_uri = (
            config_uri if config_uri is not None else storage.join(root, "config.json")
        )
        auth = CloudAuth(
            host=host,
            port=port,
            dbname=dbname,
            user=user,
            region=region,
            token_provider=token_provider,
        )

        # Inside a Batch job (AWS_BATCH_JOB_ID is injected by Batch) we ARE the worker: run the
        # experiment in THIS process, authenticating to RDS via the IAM pool. On the operator
        # seat that variable is absent, so we SUBMIT one Batch job instead — and only the submit
        # path needs the queue / job-definition names (the container never re-submits, and the
        # job definition deliberately does not bake TRIAGE_BATCH_* into the container's env).
        execution: ExecutionAdapter
        if os.environ.get("AWS_BATCH_JOB_ID"):
            execution = InProcessExecution()
        else:
            execution = BatchExecution(
                region=region,
                job_queue=_require_env("TRIAGE_BATCH_QUEUE"),
                job_definition=_require_env("TRIAGE_BATCH_JOB_DEF"),
                config_uri=cfg_uri,
            )

        return Profile(
            name="cloud",
            auth=auth,
            storage=storage,
            execution=execution,
            storage_root=root,
        )

    raise ValueError(
        f"unknown profile {name!r} — triage-pg supports 'local' and 'cloud' (ADR-0003)"
    )
