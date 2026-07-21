# Baselines — a metric floor for every problem_type

A baseline is a trivial model that runs through the full pipeline and lands on
the same leaderboard as real estimators, setting the **floor** a real model must
clear ("does the ML earn its complexity?"). Baselines are opt-in `grid_config`
class paths on the ADR-0010 score → rank → evaluate spine; nothing about them is
`problem_type`-specific except which score they emit.

The user-facing catalog with worked examples is the docs-site page
[reference/baselines](https://ccd-ia.github.io/triage-pg/reference/baselines/).
This file is the implementation index.

## Catalog

| problem_type | Estimator | Module | Notes |
| --- | --- | --- | --- |
| classification | `DummyClassifier` | `sklearn.dummy` | `strategy: prior` (deterministic base-rate). |
| classification | `BaselineRankMultiFeature`, `LinearRanker` | `catwalk/baselines/rankers.py` | Rank-by-feature heuristics (inherited). |
| classification | `SimpleThresholder` | `catwalk/baselines/thresholders.py` | Threshold-rule flagging (inherited). |
| regression | `DummyRegressor` | `sklearn.dummy` | `mean`/`median` (point) or `quantile` (τ-quantile → pinball@τ). |
| regression | `Persistence`, `PromedioDisponible`, `MovingAverage`, `Drift` | `catwalk/baselines/timeseries.py` | **Lag family** — read reserved `_target_lag_*` (windowed-label lags). No `history_query`. |
| regression | `SeasonalNaive`, `ETS`, `HoltWinters`, `Croston`, `CrostonSBA` | `catwalk/baselines/timeseries.py` | **Raw-series family** — read reserved `_hist_*` (pivoted `history_query`). ETS/HoltWinters need `triage[baselines]` (statsmodels). |
| survival | `KaplanMeierBaseline`, `NelsonAalenBaseline`, `MarginalHazardBaseline`, `SingleFeatureCox` | `catwalk/baselines/survival.py` | Marginal floors → in-PG C-index ≈ 0.5. Need `triage[survival]` (sksurv). |

## Target history (ADR-0030)

The time-series baselines consume each entity's own prior target values, exposed
point-in-time-correctly by `adapters/target_history.py` in two shapes, both
excluded from the feature set + imputation:

- **Windowed-label lags** → reserved `_target_lag_*` columns, admissible where
  `t + w ≤ as_of_date`. Config: `target_history_lags` (default 12).
- **Raw periodic series** → reserved `_hist_*` columns pivoted from an optional
  `history_query` (period-level aggregation, `knowledge_date < as_of_date`).
  Config: `history_query` (required by the raw-series family),
  `history_series_width` (default 24).

All three keys enter matrix identity. The estimator seam
(`model._history_design_X`) feeds the reserved columns to any estimator with
`consumes_target_history = True` at fit + score. See
[`docs/adr/0030-target-history-point-in-time-path.md`](adr/0030-target-history-point-in-time-path.md)
and the concept page
[concepts/target-history](https://ccd-ia.github.io/triage-pg/concepts/target-history/).

## Pinball / quantile loss (migration 0020)

`pinball@τ` scores a τ-quantile forecast — the metric a quantile forecaster
minimizes, added to the in-PG `triage.regression_metric` (ADR-0007):

```
pinball@τ = mean( τ·(y − ŷ) when y ≥ ŷ, (1 − τ)·(ŷ − y) when y < ŷ )
```

Lower is better (`triage.higher_is_better` treats `pinball@%` as a loss, so the
`metric_catalog` view + audition rank it ascending). Default regression metrics
add `pinball@0.5/0.8/0.95`. Stored as `metric='pinball@τ'`, so `evaluate_model`
needs no change. Numpy parity: `catwalk/metrics.pinball_loss`.
