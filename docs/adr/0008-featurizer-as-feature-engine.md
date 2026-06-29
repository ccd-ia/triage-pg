# 0008. featurizer (Deep Feature Synthesis) is the feature engine, replacing Collate

- Status: Accepted
- Date: 2026-06-04
- Status update (2026-06-17): **scale risk validated** — verdict *(a) scalable as-is* (per-as_of_date cost constant-to-sub-linear, linear in entities; the `cross join lateral` re-eval is the benign linear case, not superlinear). Collate not revisited. See [`docs/featurizer-scale.md`](../featurizer-scale.md) + `benchmarks/featurizer_scale.py`.
- Status update (2026-06-28): scale **re-validated** against the current pin (featurizer v0.4.1) — numbers within ~3% of the v0.3.0 baseline, verdict unchanged.

triage-pg uses **featurizer** — our modernized fork of `dssg/featurizer`, a PostgreSQL-native Deep Feature Synthesis engine — as its feature engine, replacing Collate. A deep-dive that *executed* featurizer against real PostgreSQL verified the two make-or-break properties: in one run it emits the full `cohort × as_of_dates` cross-product (triage's `(entity_id, as_of_date)` matrix shape), and its generated SQL is point-in-time-correct (no leakage). The repos split cleanly: **featurizer stays a general DFS engine**; **triage-pg owns the triage-specific adapters** (timechop→`as_of_dates`, cohort, labels, matrix assembly, cache keys). Triage concepts must never leak into featurizer.

## Considered alternatives
- *Modernize Collate in-place (PRD-09's original recommendation)* — rejected: known-correct and known-to-scale, but aging, fewer primitives, and not our actively-developed code; featurizer has a higher ceiling (114 primitives, DFS auto-synthesis) and is ours.
- *Re-express features as dbt models* — rejected: dbt has no native as-of-date parameterization; reimplementing temporal windowing in jinja is exactly where leakage bugs creep in.

## Consequences / open gaps (build, not buy)
- **Scale is the main risk to validate** (not a blocking gate — per the 2026-06-04 sequencing decision): featurizer re-evaluates aggregation CTEs once per as_of_date with no reuse across dates, so its generated SQL must be benchmarked on realistic volumes *during* feature-pipeline integration. If it can't scale and can't be fixed, revisit Collate.
- featurizer-side work: Parquet output, `>=`→`<` as-of boundary, fix degenerate window transforms, deterministic/collision-free long-name hashing, real DB-execution tests, a light imputation mechanism.
- triage-pg-side work: the adapters above + a config-driven imputation policy.
- **Imputation seam: resolved** — see ADR-0009 (fit-free in featurizer, fit-based train-fitted in triage-pg).
