# 0011. No standalone postmodeling module — diagnostics dissolve into SQL + dashboard

- Status: Accepted
- Date: 2026-06-04
- Status update (2026-06-28): Implemented — standalone postmodeling module removed; feature importances persisted at train (β/odds, migration 0009) + diagnostic SQL views + dashboard panels.
- Status update (2026-07-06): The (b)/(c) promises are now fully DELIVERED in the recorded shape (v1-release plan P4–P6, `docs/postmodeling.md`): **crosstabs** + **error analysis** land via `triage postmodel` (the matrix is read once, results persist to `triage.crosstabs`/`triage.error_analysis` — migration 0017 — and the dashboard model card reads them); **calibration** (migration 0012's function) and **windowed rollups** (0010) gained routes + panels; `individual_importances` gained its write path (per-entity β·x for linear models) and read surface; **list overlap** (Jaccard/Spearman, migration 0016) compares two models' top-k lists. Of the deferred-companion list, error trees and calibration are therefore IN; SHAP and prototype identification remain deferred. The error tree is a diagnostic only — score-modifying stacking is explicitly out of scope (v1-release plan Questionables).

triage-pg has **no standalone postmodeling module** (old triage's was ~8.8k LOC). Model analysis dissolves into three places already in the architecture: (a) **model-derived artifacts** — global/individual feature importances — are computed from the trained estimator and **persisted to PG at train time**; (b) **diagnostic SQL views** (crosstabs, error analysis, score distributions) over the predictions table (± the Parquet matrix); (c) **dashboard panels** presenting all of it. Performance, leaderboards, and bias are already SQL views per ADR-0007.

Deeper **teaching interpretability** (SHAP, error trees, calibration plots, prototype/nearest-neighbor identification) is deferred to a **separate companion** added after the core pipeline runs — explicitly not in v1.

## Considered alternatives
- *Rewrite postmodeling as a module (port the old surface)* — rejected: most of it is subsumed by the SQL-metrics layer + dashboard; a module would duplicate them.

## Consequences
- Feature importances must be written to PG during training — the only model-analysis piece that genuinely requires Python.
- The teaching-interpretability companion is a known future project, not part of triage-pg core.
