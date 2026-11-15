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
| regression | `Persistence`, `PromedioDisponible`, `MovingAverage`, `Drift` | `catwalk/baselines/timeseries.py` | **Lag family** — read reserved `_target_lag_*` (windowed-label lags on the Experiment's label-date grid). No `history_query`. `gap_policy = "calendar_positions"`. |
| regression | `SeasonalNaive`, `ETS`, `HoltWinters`, `Croston`, `CrostonSBA` | `catwalk/baselines/timeseries.py` | **Raw-series family** — read reserved `_hist_*` (the `history_query` placed by calendar offset). `requires_complete_calendar = True`, `gap_policy = "abstain"`. ETS/HoltWinters need `triage[baselines]` (statsmodels). |
| survival | `KaplanMeierBaseline`, `NelsonAalenBaseline`, `MarginalHazardBaseline`, `SingleFeatureCox` | `catwalk/baselines/survival.py` | Marginal floors → in-PG C-index ≈ 0.5. Need `triage[survival]` (sksurv). |

## Target history (ADR-0030, amended by ADR-0036)

The time-series baselines consume each entity's own prior target values, exposed
point-in-time-correctly by `adapters/target_history.py` in two shapes, both
excluded from the feature set + imputation. Both are placed on the calendar,
so a missing value leaves its own slot empty and nothing older moves into it:

- **Windowed-label lags** → reserved `_target_lag_*` columns. Lag `k` is the
  entity's label at the `k`-th admissible label date of the Experiment
  (`t + w ≤ as_of_date`), the same dates for every entity; an entity with no
  label at that date gets NULL. Config: `target_history_lags` (default 12).
- **Raw periodic series** → reserved `_hist_*` columns from an optional
  `history_query` (period-level aggregation, `knowledge_date < as_of_date`).
  Slot `_hist_{width−k}` holds the period starting `k × history_period` before
  the as_of_date. A row off that grid, or at or after the as_of_date, raises
  `HistoryPeriodMisaligned`. Config: `history_query` (required by the
  raw-series family), `history_period` (required with `history_query`),
  `history_series_width` (default 24), `fill_missing_periods`.

**Gaps.** NULL slots before an entity's first returned period are leading (the
series starts later) and are never filled. NULL slots after it are interior
gaps. With `fill_missing_periods` unset, an interior gap in any series raises
`HistoryGapsError` at matrix build, before featurizer runs, when a class with
`requires_complete_calendar = True` is in the grid; the message gives the
counts, one example and both fixes. `zero` writes 0 into interior gaps;
`null` leaves them NULL and the complete-calendar classes abstain on those
rows (a WARNING with the count). The gap counts are stored in the matrix
metadata, so a reused matrix is checked against the grid of the run that
reuses it. `triage check history CONFIG` (`diagnostics/history_check.py`)
reports the same counts without building anything.

| Class | `gap_policy` | On an interior gap |
| --- | --- | --- |
| `Persistence` | `calendar_positions` | the newest slot with a value |
| `PromedioDisponible` | `calendar_positions` | the mean of the slots with a value |
| `MovingAverage` | `calendar_positions` | the mean of the values inside the last `window` slots; NaN when all are empty |
| `Drift` | `calendar_positions` | slope over the calendar distance between the first and last values |
| `SeasonalNaive` | `abstain` | NaN when the slot one season back is a gap |
| `ETS`, `HoltWinters`, `Croston`, `CrostonSBA` | `abstain` | NaN |

The identity keys are `target_history_lags`, `history_query` and
`history_series_width` always, plus `history_layout: calendar`,
`history_period` and `fill_missing_periods` when history is attached, so no
matrix packed by rank (≤ 1.5.0) is reused, through the `logical_id` fallback
either. Matrices without history hash as they always did. The estimator seam
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

Since migration 0030 the value is computed only where `triage.models.forecast_quantile`
equals τ; every other model gets the row with a NULL value. Training writes the
column (`adapters.model.declared_quantile`: a `forecast_quantile` attribute, else
`QUANTILE_ESTIMATORS` by class name, with `DummyRegressor(strategy=median)` → 0.5),
and the migration backfilled it from `model_groups`. Stored values from before 0030
change only on re-evaluation; `triage check metrics` shows the difference.
