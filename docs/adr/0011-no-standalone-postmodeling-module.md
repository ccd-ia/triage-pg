# 0011. No standalone postmodeling module — diagnostics dissolve into SQL + dashboard

- Status: Accepted
- Date: 2026-06-04

triage-pg has **no standalone postmodeling module** (old triage's was ~8.8k LOC). Model analysis dissolves into three places already in the architecture: (a) **model-derived artifacts** — global/individual feature importances — are computed from the trained estimator and **persisted to PG at train time**; (b) **diagnostic SQL views** (crosstabs, error analysis, score distributions) over the predictions table (± the Parquet matrix); (c) **dashboard panels** presenting all of it. Performance, leaderboards, and bias are already SQL views per ADR-0007.

Deeper **teaching interpretability** (SHAP, error trees, calibration plots, prototype/nearest-neighbor identification) is deferred to a **separate companion** added after the core pipeline runs — explicitly not in v1.

## Considered alternatives
- *Rewrite postmodeling as a module (port the old surface)* — rejected: most of it is subsumed by the SQL-metrics layer + dashboard; a module would duplicate them.

## Consequences
- Feature importances must be written to PG during training — the only model-analysis piece that genuinely requires Python.
- The teaching-interpretability companion is a known future project, not part of triage-pg core.
