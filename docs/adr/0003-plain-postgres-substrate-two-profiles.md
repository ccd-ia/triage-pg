# 0003. Plain-PostgreSQL substrate with local/cloud deployment profiles

- Status: Accepted
- Date: 2026-06-04

triage-pg targets **plain PostgreSQL with no proprietary extensions**, so the schema, SQL, and PL/pgSQL run identically on a laptop, Docker, a self-hosted server, or RDS. Environment-specific concerns are isolated behind three small swappable adapters — **auth** (password/local vs IAM), **storage** (local filesystem vs S3), **execution** (in-process vs AWS Batch) — yielding a **local/default** profile (standalone PG; dev, teaching, tests, client-own-PG) and a **cloud** profile (the ccd-ia hosted instance). Standalone PG is non-negotiable: the test suite (`pytest-postgresql`/CI) and the DirtyDuck tutorial run against local Postgres, where IAM auth cannot exist — so the cloud profile is always an *additional* path, never a replacement.

## Consequences
- `pg_duckdb`/`pg_parquet` are out (RDS can't run them); matrices are Parquet read into the compute container, and the important in-PG compute (eval/leaderboards/bias over the predictions table) needs no extensions.
- A future on-prem/other-cloud requirement changes only the auth/execution adapters, not the core.

## Status update (2026-06-20) — seam specified

The three adapters this ADR named are now specified in `docs/cloud-profile-spec.md` (the cloud-profile analog of `docs/adapter-spec.md`). Resolved shape: a frozen `Profile` value object bundles `{auth, storage, execution}`, built at the CLI boundary from `--profile` + environment variables; `auth` produces the `ConnectionPool` and `execution` wraps submit-or-run *around* the headless core, while only `storage` threads *into* it (replacing `run_experiment`'s `storage_dir: str`). Cloud impls: IAM auth refreshes the RDS token **per physical connection** via a psycopg `connection_class`; storage is one fsspec/pyarrow scheme adapter (local-vs-`s3://`) that also absorbs GC's file deletion; execution is one Batch job per experiment, the container authenticating via its IAM **task role** (no credentials passed), with Batch/RDS/IAM infra provisioned out-of-band (Terraform). Grid parallelism (ADR-0005) is deferred — see ADR-0020.
