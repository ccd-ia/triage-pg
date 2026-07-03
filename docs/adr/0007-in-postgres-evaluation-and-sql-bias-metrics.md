# 0007. Evaluation, leaderboards, and bias metrics computed in PostgreSQL

- Status: Accepted
- Date: 2026-06-04
- Status update (2026-06-28): Implemented — PL/pgSQL precision@k/recall@k/auc_roc/average_precision/regression + `compute_bias_metrics` (migration 0002); `component/catwalk/in_pg_evaluation.py`. Aequitas dropped for SQL group-bys over `triage.protected_groups`.
- Status update (2026-07-03): The "validate against known Aequitas outputs" consequence is closed with a **recorded waiver** — Aequitas is pandas-2-incompatible and cannot run to produce reference outputs (the very reason it was dropped). The SQL bias group-bys are instead validated on hand-computed fixtures matching Aequitas' definitions (per-group selection_rate; disparity ratio vs the largest-group reference; explicit reference override): `src/tests/catwalk_tests/test_in_pg_metrics.py::test_bias_metrics_group_by_and_disparity` / `::test_bias_metrics_explicit_reference_group`. Note: the inherited Python `component/audition` survives as a CLI thin client over greenfield tables (the dashboard uses the SQL catalog views directly) — flagged for accept-or-retire in `docs/adr-conformance.md`.

Evaluation metrics (precision@k, recall@k, AUC, and regression RMSE/MAE/R²), model leaderboards, and audition run as **PL/pgSQL functions plus (materialized) views over the predictions table**, not in Python — they need only `(entity_id, score, label)`, which lives in PostgreSQL regardless of where matrices are stored. **Bias/fairness metrics are reimplemented as SQL group-bys**, dropping the Aequitas library.

## Considered alternatives
- *Compute metrics in pandas/Python (as current triage does)* — rejected: the predictions table is small and SQL-shaped; in-PG keeps dashboards instant and Python-free.
- *Keep the Aequitas library* — rejected: it is pandas-2.x-incompatible (already disabled), and disparity metrics are plain group-bys once evaluation lives in SQL; reimplementing drops a broken dependency.

## Consequences
- A SQL bias-metrics implementation must be written and validated against known Aequitas outputs.
