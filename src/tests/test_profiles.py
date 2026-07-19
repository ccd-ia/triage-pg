"""Cloud-profile adapter tests — auth / storage / execution, with mocked AWS (no live AWS).

The testing contract of cloud-profile-spec §6: the cloud adapters are exercised entirely against
``moto`` (``mock_aws``) + a stubbed RDS-IAM token provider, so the suite never touches live AWS.
The local-PG suite (``pytest-postgresql``) stays the non-negotiable substrate and is unaffected —
nothing here needs a database.

Covered:
* **auth** — the IAM ``connection_class`` injects the stub token into ``password`` and forces
  ``sslmode=verify-full`` + the RDS CA bundle (no real RDS).
* **storage** — the FS branch over ``tmp_path`` and the S3 branch over ``moto`` each write, read,
  and delete a Parquet, asserting the scheme dispatch.
* **execution** — ``BatchExecution.run`` over ``moto`` Batch+S3 stages the config and
  ``submit_job``s with the config URI + profile in its parameters.
* **profile** — ``load_profile('cloud')`` builds the cloud impls from env and fail-fasts (naming
  the var) on a missing one; ``load_profile('local')`` builds the local impls.
"""

from __future__ import annotations

from typing import Any, cast

import boto3
import polars as pl
import pytest
from moto import mock_aws

from triage.profiles import (
    BatchExecution,
    CloudAuth,
    InProcessExecution,
    LocalAuth,
    LocalStorage,
    S3Storage,
    load_profile,
)
from triage.profiles.auth import make_iam_connection_class
from triage.profiles.storage import (
    parent_root,
    read_parquet,
    storage_for_root,
    write_parquet,
)
from triage.util.db import DictRowPool

REGION = "us-east-1"
BUCKET = "triage-test-bucket"


# ----------------------------------------------------------------------------- AWS env / clients


@pytest.fixture
def aws_credentials(monkeypatch):
    """Dummy AWS creds so botocore/s3fs never reach real AWS even if moto is bypassed."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)


def _provision_batch(region: str) -> None:
    """Stand up the minimal Batch infra moto needs for ``submit_job`` (queue + job definition)."""
    ec2 = boto3.client("ec2", region_name=region)
    iam = boto3.client("iam", region_name=region)
    batch = boto3.client("batch", region_name=region)

    vpc = ec2.create_vpc(CidrBlock="10.0.0.0/16")["Vpc"]["VpcId"]
    ec2.create_subnet(VpcId=vpc, CidrBlock="10.0.0.0/24")
    ec2.create_security_group(GroupName="triage-sg", Description="d", VpcId=vpc)
    role = iam.create_role(RoleName="triage-batch-role", AssumeRolePolicyDocument="{}")[
        "Role"
    ]["Arn"]
    compute_env = batch.create_compute_environment(
        computeEnvironmentName="triage-compute-env",
        type="UNMANAGED",
        state="ENABLED",
        serviceRole=role,
    )["computeEnvironmentArn"]
    batch.create_job_queue(
        jobQueueName="triage-queue",
        state="ENABLED",
        priority=1,
        computeEnvironmentOrder=[{"order": 1, "computeEnvironment": compute_env}],
    )
    batch.register_job_definition(
        jobDefinitionName="triage-jobdef",
        type="container",
        containerProperties={"image": "triage:latest", "vcpus": 1, "memory": 256},
    )


# ----------------------------------------------------------------------------------------- auth


def test_iam_connection_class_injects_token_and_forces_tls(monkeypatch):
    """The IAM connection_class stamps the (stub) token as the password + verify-full + CA bundle."""
    captured: dict[str, Any] = {}

    def fake_connect(cls, conninfo="", **kwargs):
        captured["conninfo"] = conninfo
        captured.update(kwargs)
        return "CONNECTED"

    # Patch the real psycopg connect so no DB is touched; assert what the subclass passed up.
    import psycopg

    monkeypatch.setattr(psycopg.Connection, "connect", classmethod(fake_connect))

    connection_class = make_iam_connection_class(
        lambda: "STUB-TOKEN", sslrootcert="/etc/ssl/rds-ca.pem"
    )
    result = connection_class.connect("host=h dbname=d user=u")

    assert result == "CONNECTED"
    assert captured["password"] == "STUB-TOKEN"
    assert captured["sslmode"] == "verify-full"
    assert captured["sslrootcert"] == "/etc/ssl/rds-ca.pem"
    assert captured["conninfo"] == "host=h dbname=d user=u"


def test_cloud_auth_uses_injected_token_provider(monkeypatch):
    """CloudAuth.open_pool builds a pool whose connection_class carries the injected provider.

    We stop short of opening a real connection (there is no RDS): patch ConnectionPool to capture
    its kwargs and assert the password-less conninfo + the IAM connection_class + max_lifetime.
    """
    calls: dict[str, Any] = {}

    class FakePool:
        def __init__(self, conninfo, **kwargs):
            calls["conninfo"] = conninfo
            calls.update(kwargs)

    monkeypatch.setattr("triage.profiles.auth.ConnectionPool", FakePool)

    auth = CloudAuth(
        host="db.example.com",
        port=5432,
        dbname="proj",
        user="iam_user",
        region=REGION,
        token_provider=lambda: "TOKEN",
    )
    auth.open_pool(min_size=2, max_size=7)

    assert "password" not in calls["conninfo"]
    assert "host=db.example.com" in calls["conninfo"]
    assert "user=iam_user" in calls["conninfo"]
    assert calls["max_lifetime"] == 600
    assert calls["min_size"] == 2 and calls["max_size"] == 7
    # the connection_class is the IAM subclass — connecting through it would call our provider.
    assert issubclass(calls["connection_class"], __import__("psycopg").Connection)


# -------------------------------------------------------------------------------------- storage


def test_local_storage_parquet_roundtrip_and_delete(tmp_path):
    storage = LocalStorage()
    uri = storage.join(str(tmp_path / "matrices"), "m.parquet")
    frame = pl.DataFrame({"entity_id": [1, 2], "f": [0.5, 0.6]})

    write_parquet(storage, uri, frame)
    back = read_parquet(storage, uri)

    assert back.sort("entity_id").to_dict(as_series=False) == frame.to_dict(
        as_series=False
    )
    assert storage.delete(uri) is True
    assert storage.delete(uri) is False  # already absent


def test_storage_for_root_dispatches_by_scheme():
    assert isinstance(storage_for_root("./matrices"), LocalStorage)
    assert isinstance(storage_for_root("file:///tmp/x"), LocalStorage)
    assert isinstance(storage_for_root("s3://bucket/scope"), S3Storage)
    with pytest.raises(ValueError, match="unsupported storage scheme"):
        storage_for_root("gs://bucket/scope")


def test_parent_root_recovers_storage_root_per_scheme():
    """The flat layout means an artifact URI's parent IS the storage root — all schemes."""
    assert parent_root("/data/store/abc123.joblib") == "/data/store"
    assert parent_root("./store/abc123.parquet") == "store"
    assert parent_root("s3://bucket/scope/abc123.joblib") == "s3://bucket/scope"
    assert parent_root("file:///data/store/abc123.joblib") == "file:///data/store"


@mock_aws
def test_s3_storage_parquet_roundtrip_and_delete(aws_credentials):
    boto3.client("s3", region_name=REGION).create_bucket(Bucket=BUCKET)
    storage = S3Storage(region=REGION)
    uri = storage.join(f"s3://{BUCKET}/scope", "m.parquet")
    frame = pl.DataFrame({"entity_id": [1, 2, 3], "f": [0.1, 0.2, 0.3]})

    write_parquet(storage, uri, frame)
    back = read_parquet(storage, uri)

    assert back.height == 3
    assert set(back["entity_id"].to_list()) == {1, 2, 3}
    assert storage.delete(uri) is True
    assert storage.delete(uri) is False


@mock_aws
def test_s3_storage_write_bytes_and_open_input(aws_credentials):
    boto3.client("s3", region_name=REGION).create_bucket(Bucket=BUCKET)
    storage = S3Storage(region=REGION)
    uri = f"s3://{BUCKET}/scope/config.json"

    storage.write_bytes(uri, b'{"problem_type": "classification"}')
    with storage.open_input(uri) as handle:
        assert handle.read() == b'{"problem_type": "classification"}'


# ------------------------------------------------------------------------------------ execution


def test_in_process_execution_calls_run_experiment(monkeypatch):
    """InProcessExecution.run threads storage/storage_root into run_experiment synchronously."""
    captured: dict[str, Any] = {}

    def fake_run_experiment(pool, config, *, storage, storage_root, **kw):
        captured.update(
            pool=pool, config=config, storage=storage, storage_root=storage_root, **kw
        )
        return "RUN-RESULT"

    monkeypatch.setattr("triage.adapters.run.run_experiment", fake_run_experiment)

    storage = LocalStorage()
    handle = InProcessExecution().run(
        # local profile doesn't touch the pool (run_experiment is monkeypatched); a
        # sentinel string stands in for the DictRowPool the signature asks for.
        cast(DictRowPool, cast(object, "POOL")),
        {"problem_type": "classification"},
        storage=storage,
        storage_root="./store",
        random_seed=7,
    )

    assert handle.run_result == "RUN-RESULT"
    assert handle.batch_job_id is None
    assert captured["storage"] is storage
    assert captured["storage_root"] == "./store"
    assert captured["random_seed"] == 7


@mock_aws
def test_batch_execution_stages_config_and_submits(aws_credentials):
    boto3.client("s3", region_name=REGION).create_bucket(Bucket=BUCKET)
    _provision_batch(REGION)

    storage = S3Storage(region=REGION)
    config_uri = f"s3://{BUCKET}/scope/config.json"
    execution = BatchExecution(
        region=REGION,
        job_queue="triage-queue",
        job_definition="triage-jobdef",
        config_uri=config_uri,
    )

    handle = execution.run(
        # cloud profile opens its own pool inside the Batch job (ADR-0005), so None here.
        cast(DictRowPool, cast(object, None)),
        {"problem_type": "classification", "grid_config": {"x": {}}},
        storage=storage,
        storage_root=f"s3://{BUCKET}/scope",
    )

    # returns immediately with a job id (async submit-and-return)
    assert handle.batch_job_id is not None
    assert handle.run_result is None
    assert handle.config_uri == config_uri

    # the config was staged to S3
    body = (
        boto3.client("s3", region_name=REGION)
        .get_object(Bucket=BUCKET, Key="scope/config.json")["Body"]
        .read()
    )
    assert b"classification" in body

    # the submitted job's parameters carry the config URI + the cloud profile
    jobs = boto3.client("batch", region_name=REGION).describe_jobs(
        jobs=[handle.batch_job_id]
    )["jobs"]
    assert jobs[0]["parameters"]["config_uri"] == config_uri
    assert jobs[0]["parameters"]["profile"] == "cloud"


@mock_aws
def test_batch_job_status_reads_and_handles_unknown(aws_credentials):
    """The §7 status backfill read: a real job reports its Batch status; an unknown id is
    UNKNOWN (never an exception — Batch history expires, the run row may outlive it)."""
    from triage.profiles.execution import batch_job_status

    boto3.client("s3", region_name=REGION).create_bucket(Bucket=BUCKET)
    _provision_batch(REGION)
    execution = BatchExecution(
        region=REGION,
        job_queue="triage-queue",
        job_definition="triage-jobdef",
        config_uri=f"s3://{BUCKET}/scope/config.json",
    )
    handle = execution.run(
        # cloud profile opens its own pool inside the Batch job (ADR-0005), so None here.
        cast(DictRowPool, cast(object, None)),
        {"problem_type": "classification"},
        storage=S3Storage(region=REGION),
        storage_root=f"s3://{BUCKET}/scope",
    )

    assert handle.batch_job_id is not None
    info = batch_job_status(handle.batch_job_id, region=REGION)
    assert info["job_id"] == handle.batch_job_id
    # moto walks the job through the Batch lifecycle; any real lifecycle state is fine here —
    # the contract under test is shape + no-exception, not moto's scheduler timing.
    assert info["status"] in (
        "SUBMITTED",
        "PENDING",
        "RUNNABLE",
        "STARTING",
        "RUNNING",
        "SUCCEEDED",
        "FAILED",
    )

    missing = batch_job_status("no-such-job-id", region=REGION)
    assert missing["status"] == "UNKNOWN"
    assert "not found" in missing["reason"]


# -------------------------------------------------------------------------------------- profile


def _set_cloud_env(monkeypatch) -> None:
    monkeypatch.setenv("AWS_REGION", REGION)
    monkeypatch.setenv("TRIAGE_RDS_HOST", "db.example.com")
    monkeypatch.setenv("TRIAGE_RDS_PORT", "5432")
    monkeypatch.setenv("TRIAGE_RDS_DB", "proj")
    monkeypatch.setenv("TRIAGE_RDS_USER", "iam_user")
    monkeypatch.setenv("TRIAGE_S3_BUCKET", BUCKET)
    monkeypatch.setenv("TRIAGE_BATCH_QUEUE", "triage-queue")
    monkeypatch.setenv("TRIAGE_BATCH_JOB_DEF", "triage-jobdef")


def test_load_profile_local_builds_local_impls():
    profile = load_profile(
        "local", dburl="postgresql+psycopg://u@h:5432/d", storage_root="./store"
    )
    assert profile.name == "local"
    assert isinstance(profile.auth, LocalAuth)
    assert isinstance(profile.storage, LocalStorage)
    assert isinstance(profile.execution, InProcessExecution)
    assert profile.storage_root == "./store"


def test_load_profile_cloud_builds_cloud_impls(monkeypatch):
    _set_cloud_env(monkeypatch)
    profile = load_profile("cloud", token_provider=lambda: "TOKEN")

    assert profile.name == "cloud"
    assert isinstance(profile.auth, CloudAuth)
    assert isinstance(profile.storage, S3Storage)
    assert isinstance(profile.execution, BatchExecution)
    assert profile.storage_root == f"s3://{BUCKET}"
    # config_uri defaults under the storage root
    assert profile.execution._config_uri == f"s3://{BUCKET}/config.json"


def test_load_profile_cloud_fail_fasts_on_missing_env(monkeypatch):
    _set_cloud_env(monkeypatch)
    monkeypatch.delenv("TRIAGE_BATCH_QUEUE")
    with pytest.raises(ValueError, match="TRIAGE_BATCH_QUEUE"):
        load_profile("cloud")


def test_load_profile_cloud_inside_batch_job_runs_in_process(monkeypatch):
    """Inside a Batch job (AWS_BATCH_JOB_ID set) the cloud profile runs the experiment IN-PROCESS
    against RDS via IAM — it must NOT build a BatchExecution (which would re-submit the job).
    """
    _set_cloud_env(monkeypatch)
    monkeypatch.setenv("AWS_BATCH_JOB_ID", "abc-123")
    profile = load_profile("cloud", token_provider=lambda: "TOKEN")

    assert isinstance(profile.execution, InProcessExecution)
    assert isinstance(profile.auth, CloudAuth)  # the worker opens its own IAM pool
    assert isinstance(profile.storage, S3Storage)


def test_load_profile_cloud_inside_batch_job_needs_no_batch_names(monkeypatch):
    """The container's env deliberately omits TRIAGE_BATCH_* (the job def bakes only cluster
    vars); the in-process worker path must not require them — only the submit path does.
    """
    _set_cloud_env(monkeypatch)
    monkeypatch.setenv("AWS_BATCH_JOB_ID", "abc-123")
    monkeypatch.delenv("TRIAGE_BATCH_QUEUE")
    monkeypatch.delenv("TRIAGE_BATCH_JOB_DEF")

    profile = load_profile("cloud", token_provider=lambda: "TOKEN")
    assert isinstance(profile.execution, InProcessExecution)


def test_load_profile_rejects_unknown_name():
    with pytest.raises(ValueError, match="unknown profile"):
        load_profile("onprem", dburl="postgresql://u@h/d")
