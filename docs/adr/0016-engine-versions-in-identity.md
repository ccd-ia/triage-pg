# 0016. Engine versions enter identity per kind; strict with logical fallback

- Status: Accepted
- Date: 2026-06-11

A version enters a derivation hash (ADR-0013) iff it can change the artifact's
**output bytes given identical config and inputs** — the compiler-vs-runtime
criterion. The *compilers* enter, per kind: triage-pg on every node, featurizer
additionally on feature groups (same config → different SQL across versions),
and the estimator's distribution additionally on models (same matrix +
hyperparameters + seed → different coefficients; sklearn guarantees no
cross-version equivalence). The *runtimes* — PostgreSQL and Python — are
excluded from identity and recorded at the run level, accepting documented
residual risks (float aggregation order, collations; the ranking path is
already shielded by the deterministic entity_id tiebreak). Identity uses
**release versions**, not git hashes (a behavior-changing dev edit requires a
version bump or `--force`; `runs.git_hash` keeps forensics). Versions always
hash — reuse across engine drift is only available through the opt-in
`policy='logical'` fallback, which matches on `artifacts.logical_id` (a second
Merkle chain computed without engine versions over the parents' *logical* ids)
and emits a loud ENGINE-DRIFT REUSE warning.

## Considered alternatives
- *No engine versions in identity (record-only, like PG/Python)* — rejected:
  after an upgrade the cache silently serves the old behavior's outputs;
  invisible wrongness vs a visible, bounded rebuild.
- *Git hash in identity* — rejected: invalidates all caches on every commit;
  disables caching while developing triage-pg itself.
- *Full environment manifest (uv.lock hash)* — rejected: any dependency bump,
  even a dev tool, rebuilds the entire store.
- *Manual engine epochs (cache salts)* — rejected: relies on humans
  remembering — the failure mode declared pinning exists to remove.

## Consequences
- An sklearn bump rebuilds models only; a featurizer bump rebuilds feature
  groups and everything downstream — invalidation propagates via DAG edges.
- `triage.artifacts` carries an indexed `logical_id`; `derive()` returns both
  chains; `engine_versions_for(kind, estimator_class_path)` encodes the map.
- Laptop↔cloud engine skew fragments the cache by default; `policy='logical'`
  is the explicit, warned escape hatch.
