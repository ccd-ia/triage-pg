# 0013. Artifact identity is a derivation hash over the full input closure

- Status: Accepted
- Date: 2026-06-11

Every built artifact (cohort, labels, features, matrix, model, prediction run,
evaluation) is identified by a **derivation hash** à la Guix: SHA-256 over its
canonicalized own config, the derivation ids of its **parent artifacts**
(Merkle DAG), its **source-data pins** (ADR-0014), and the engine versions.
Building becomes lookup-or-create — a present derivation id with an existing
output is a cache hit — which replaces the inherited `replace`-flag contract
("if the source data has changed, set `replace=True`") whose hashes covered
config text only and missed data, code, and upstream changes entirely. The DAG
lives in the per-project PostgreSQL schema so provenance, staleness, and GC
(experiments as roots) are plain SQL, per the headless-complete core (ADR-0012).

## Considered alternatives
- *Inherited config-text hashing + `replace` flag* — rejected: cache validity
  by human vigilance; no closure, no lineage, no GC.
- *Orchestrator-owned lineage (Dagster asset versioning, dbt state)* —
  rejected: ties artifact identity to a deployment choice and hides it from
  SQL; triage-pg core must stay self-contained (ADR-0003, ADR-0012).

## Consequences
- Identity scheme bakes into every stored hash — changing it later orphans all
  cached artifacts (they rebuild; nothing breaks, but history loses dedup).
- Predictions stay append-only **events** with lineage, never deduplicated
  cache entries (ADR-0006 unchanged).
- Node granularity, engine-version policy, and GC retention are specified in
  `docs/derivation-dag.md` (§4 open at time of writing).
