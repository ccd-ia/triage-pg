# 0004. Cloud profile uses RDS/Aurora with IAM database authentication

- Status: Accepted
- Date: 2026-06-04

The cloud profile runs managed PostgreSQL (RDS/Aurora) and authenticates via **IAM database authentication**: per-project PG roles + per-project IAM roles, with RDS issuing short-lived tokens — **no database passwords stored anywhere**. This is the only mechanism that gives per-project credential scoping to ephemeral AWS Batch jobs (which run semi-trusted code, since experiment configs instantiate arbitrary Python classes) while honoring the no-plaintext-secrets rule, and it removes DBA toil (HA/backups/PITR are managed).

## Considered alternatives
- *Self-managed PostgreSQL (to keep `pg_duckdb`)* — rejected: forces hand-rolled per-project roles + a Secrets-Manager reference per project + rotation (more secret plumbing) plus ongoing ops toil; `pg_duckdb` was only ever a matrix-as-SQL nice-to-have.
- *One shared cluster-wide credential for all Batch jobs* — rejected: a buggy or curious job for one project could read another client's database.

## Consequences
- Accepts AWS lock-in for the cloud profile (IAM auth + Batch don't port to on-prem/GCP); mitigated by the adapter seam (ADR-0003).
