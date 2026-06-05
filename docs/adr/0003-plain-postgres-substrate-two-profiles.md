# 0003. Plain-PostgreSQL substrate with local/cloud deployment profiles

- Status: Accepted
- Date: 2026-06-04

triage-pg targets **plain PostgreSQL with no proprietary extensions**, so the schema, SQL, and PL/pgSQL run identically on a laptop, Docker, a self-hosted server, or RDS. Environment-specific concerns are isolated behind three small swappable adapters — **auth** (password/local vs IAM), **storage** (local filesystem vs S3), **execution** (in-process vs AWS Batch) — yielding a **local/default** profile (standalone PG; dev, teaching, tests, client-own-PG) and a **cloud** profile (the ccd-ia hosted instance). Standalone PG is non-negotiable: the test suite (`pytest-postgresql`/CI) and the DirtyDuck tutorial run against local Postgres, where IAM auth cannot exist — so the cloud profile is always an *additional* path, never a replacement.

## Consequences
- `pg_duckdb`/`pg_parquet` are out (RDS can't run them); matrices are Parquet read into the compute container, and the important in-PG compute (eval/leaderboards/bias over the predictions table) needs no extensions.
- A future on-prem/other-cloud requirement changes only the auth/execution adapters, not the core.
