# 0007. Evaluation, leaderboards, and bias metrics computed in PostgreSQL

- Status: Accepted
- Date: 2026-06-04

Evaluation metrics (precision@k, recall@k, AUC, and regression RMSE/MAE/R²), model leaderboards, and audition run as **PL/pgSQL functions plus (materialized) views over the predictions table**, not in Python — they need only `(entity_id, score, label)`, which lives in PostgreSQL regardless of where matrices are stored. **Bias/fairness metrics are reimplemented as SQL group-bys**, dropping the Aequitas library.

## Considered alternatives
- *Compute metrics in pandas/Python (as current triage does)* — rejected: the predictions table is small and SQL-shaped; in-PG keeps dashboards instant and Python-free.
- *Keep the Aequitas library* — rejected: it is pandas-2.x-incompatible (already disabled), and disparity metrics are plain group-bys once evaluation lives in SQL; reimplementing drops a broken dependency.

## Consequences
- A SQL bias-metrics implementation must be written and validated against known Aequitas outputs.
