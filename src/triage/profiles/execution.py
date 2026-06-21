"""Execution adapters — the in-process / AWS-Batch submit seam (ADR-0005, cloud-profile-spec §4).

A thin **submit-or-run switch** wrapped *around* :func:`triage.adapters.run.run_experiment`; it
does not restructure the core. Local runs the experiment synchronously and returns its
``RunResult``. Cloud writes the config to S3, submits one Batch job per experiment, and returns
**immediately** with the Batch ``job_id`` (async; the operator polls) — the container then runs
``triage run --profile cloud --config <s3uri>`` and authenticates via its IAM task role, so no
credentials ever cross the wire.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from psycopg_pool import ConnectionPool
from triage.profiles.protocols import StorageAdapter

from triage.derivation import canonical_json
from triage.logging import get_logger

logger = get_logger(__name__)

__all__ = ["RunHandle", "InProcessExecution", "BatchExecution"]


@dataclass(frozen=True)
class RunHandle:
    """The outcome of an execution. Local carries the in-process ``RunResult``; cloud carries
    the Batch ``job_id`` (the run completes asynchronously inside the job)."""

    run_result: Any | None = None
    batch_job_id: str | None = None
    config_uri: str | None = None


class InProcessExecution:
    """Local execution: call :func:`run_experiment` synchronously in this process."""

    def run(
        self,
        pool: ConnectionPool,
        experiment_config: Mapping[str, Any],
        *,
        storage: StorageAdapter,
        storage_root: str,
        **run_kwargs: Any,
    ) -> RunHandle:
        from triage.adapters.run import run_experiment

        result = run_experiment(
            pool,
            experiment_config,
            storage=storage,
            storage_root=storage_root,
            **run_kwargs,
        )
        return RunHandle(run_result=result)


class BatchExecution:
    """Cloud execution: stage the config to S3 and ``submit_job`` against pre-provisioned Batch.

    The queue / job-definition names and the S3 scope come from the environment (cloud-profile
    -spec §5); the infra itself is provisioned out of band (Terraform, ADR-0005). The submitted
    container authenticates via its IAM task role (no secrets passed), reads the config from S3,
    and runs the experiment in-process (it uses :class:`InProcessExecution` inside the job).
    """

    def __init__(
        self,
        *,
        region: str,
        job_queue: str,
        job_definition: str,
        config_uri: str,
        profile_name: str = "cloud",
    ) -> None:
        self._region = region
        self._job_queue = job_queue
        self._job_definition = job_definition
        self._config_uri = config_uri
        self._profile_name = profile_name

    def run(
        self,
        pool: ConnectionPool,  # noqa: ARG002 — unused in cloud (the job opens its own pool)
        experiment_config: Mapping[str, Any],
        *,
        storage: StorageAdapter,
        storage_root: str,  # noqa: ARG002 — the job reads storage_root from its own profile/env
        **run_kwargs: Any,  # noqa: ARG002 — threaded by the in-job InProcessExecution, not here
    ) -> RunHandle:
        import boto3

        # 1. Serialize the config (canonical JSON) and stage it to S3 for the container to read.
        config_bytes = canonical_json(dict(experiment_config)).encode("utf-8")
        storage.write_bytes(self._config_uri, config_bytes)
        logger.info(f"Staged experiment config to {self._config_uri}")

        # 2. Submit one Batch job per experiment; the container entrypoint is
        #    ``triage run --profile cloud --config <config_uri>`` (parameters consumed by the
        #    job definition's command template). NO credentials are passed — the task role grants
        #    RDS-IAM-connect + S3 access.
        batch = boto3.client("batch", region_name=self._region)
        response = batch.submit_job(
            jobName="triage-experiment",
            jobQueue=self._job_queue,
            jobDefinition=self._job_definition,
            parameters={
                "config_uri": self._config_uri,
                "profile": self._profile_name,
            },
        )
        job_id = response["jobId"]
        logger.info(
            f"Submitted Batch job {job_id} (queue={self._job_queue},"
            + f" jobDef={self._job_definition}); returning immediately (async)"
        )

        # 3. Return immediately — the CLI does NOT block on a Batch job (ADR-0005); the operator
        #    polls. The job_id is recorded on triage.runs.batch_job_id by the caller.
        return RunHandle(batch_job_id=job_id, config_uri=self._config_uri)
