---
title: Baselines
description: A metric floor for every problem_type — the trivial models a real model must beat. What they are, how to configure them, and how to read the floor-vs-model gap.
sidebar:
  order: 6
---

A **baseline** is a deliberately trivial model. It sits in the same grid as your
real estimators, runs through the same cohort → features → matrices → train →
predict → evaluate pipeline, and lands on the same leaderboard. Its only job is
to set the **floor**: the score a real model must clear to have earned its
complexity. A random forest at 0.57 precision@100 sounds fine until the
constant-prior baseline is at 0.55 — then the ML bought you almost nothing.

Because the score → rank → evaluate spine is `problem_type`-agnostic
([ranking spine](/triage-pg/concepts/problem-types-and-ranking/)), a baseline is
"just another estimator" — a `class_path` in `grid_config`. This page is the
catalog: one section per `problem_type`, each estimator's class path, its
parameters, when to reach for it, and how to read its floor metric.

## The idea in one line

> If your model can't beat a baseline that ignores the features (or uses just
> one), the features — or the modeling — are not earning their keep.

Every baseline is **fast** (no real fitting) and most are **deterministic**, so
the floor is reproducible run to run. Add them once and the "does the ML earn
its complexity?" question is answered on every leaderboard.

## Classification

Continuous score → rank → precision@k / recall@k / AUC. The floor is the
base-rate performance of a model that has learned nothing about individuals.

| Estimator (`class_path`) | Params | What it does |
| --- | --- | --- |
| `sklearn.dummy.DummyClassifier` | `strategy: [prior]` | Constant class-prior probability. `prior`/`most_frequent` are deterministic; avoid `stratified` (random) if you want a stable floor. |
| `triage.component.catwalk.baselines.rankers.BaselineRankMultiFeature` | `rules` | Rank by one or more raw features with an explicit sort direction — the "expert heuristic" floor. |
| `triage.component.catwalk.baselines.rankers.LinearRanker` | `features`, `weights` | Rank by a hand-weighted linear combination of features. |
| `triage.component.catwalk.baselines.thresholders.SimpleThresholder` | `rules` | Flag on simple threshold rules. |

```yaml
grid_config:
  'sklearn.tree.DecisionTreeClassifier': { max_depth: [3, 5] }
  # the floor: does the tree beat a constant-prior guess?
  'sklearn.dummy.DummyClassifier':
    strategy: ['prior']
```

**Reading the floor.** On the leaderboard, `DummyClassifier`'s precision@k is
the cohort's base rate at that k. If your model's precision@k is not comfortably
above it, the features aren't separating the classes.

## Regression & regression_ranking

The target is continuous (a count, a duration, a demand). `regression_ranking`
(the primary continuous path) ranks entities by the predicted value;
`regression` scores it directly with **rmse / mae / r2 / [pinball@τ](#pinball--quantile-loss)**.
There are two families of regression baseline, split by *which* history they
read.

### Cross-sectional floor (no history)

| Estimator | Params | What it does |
| --- | --- | --- |
| `sklearn.dummy.DummyRegressor` | `strategy: [mean, median]` | Predicts a constant statistic of the training target. RMSE floor = std(y); a degenerate constant ranking. |
| `sklearn.dummy.DummyRegressor` | `strategy: [quantile], quantile: [0.9]` | Predicts a constant **τ-quantile** — the trivial τ-quantile forecaster, scored by `pinball@τ`. |

### Time-series floors (per-entity target history)

These forecast each entity's next-window target from its **own prior target
values** ([target history](/triage-pg/concepts/target-history/)). Two
sub-families by which history shape they read.

> In the tables and snippets below, `…` abbreviates `triage.component.catwalk` —
> the full class path is e.g. `triage.component.catwalk.baselines.timeseries.Persistence`.

**Lag family** — read the windowed-label lags (the reserved `_target_lag_*`
columns). They work off the label; no `history_query` needed. Set
`target_history_lags: N` (default 12).

| Estimator | Params | Forecast |
| --- | --- | --- |
| `…baselines.timeseries.Persistence` | — | Last observed value (`y_{t-1}`). |
| `…baselines.timeseries.PromedioDisponible` | — | Mean of available history. |
| `…baselines.timeseries.MovingAverage` | `window: [3, 6, 12]` | Mean of the last `window` values. |
| `…baselines.timeseries.Drift` | — | Last value + the average per-step trend. |

**Raw-series family** — forecast the raw periodic series (e.g. monthly counts)
supplied by a `history_query`. These **require** a `history_query` (enforced at
run start) and the exponential-smoothing pair needs `statsmodels` (the
`baselines` extra: `uv sync --extra baselines`).

| Estimator | Params | Forecast |
| --- | --- | --- |
| `…baselines.timeseries.SeasonalNaive` | `season: [12]` | The value one season back (`y_{t-season}`). |
| `…baselines.timeseries.ETS` | `alpha` | Simple exponential smoothing (level). |
| `…baselines.timeseries.HoltWinters` | `trend`, `seasonal`, `seasonal_periods` | Trend (+ optional seasonality) smoothing. |
| `…baselines.timeseries.Croston` | `alpha: [0.1]` | Intermittent-demand rate (Croston's method). |
| `…baselines.timeseries.CrostonSBA` | `alpha: [0.1]` | Croston debiased by `1 − α/2` (Syntetos–Boylan). |

```yaml
problem_type: regression_ranking

# the raw periodic series the raw-series baselines forecast over: per-entity
# MONTHLY counts, point-in-time correct (only events known before {as_of_date}).
target_history_lags: 6
history_series_width: 24
history_query: |
  select entity_id, date_trunc('month', date)::date as period, count(*) as value
  from ontology.events
  where date < {as_of_date}
  group by 1, 2

grid_config:
  'sklearn.ensemble.RandomForestRegressor': { n_estimators: [100] }
  # lag floors (read _target_lag_*; no history_query needed)
  '…baselines.timeseries.Persistence': {}
  '…baselines.timeseries.MovingAverage': { window: [3, 6] }
  '…baselines.timeseries.Drift': {}
  # raw-series floors (need history_query above)
  '…baselines.timeseries.SeasonalNaive': { season: [12] }
  '…baselines.timeseries.HoltWinters': {}
  '…baselines.timeseries.Croston': {}
```

**Cold start.** An entity with no usable history (e.g. the earliest as_of_dates,
before any label window has closed) can't be forecast — the baseline **abstains**:
it emits no prediction for that entity, so the metric is computed over the rows it
*could* score (a baseline never fabricates a future value). This is why a baseline's
`num_labeled` on the leaderboard can be slightly below a real model's.

## Survival

Rank entities by risk (higher = event sooner) → in-PG **C-index** (concordance).
The marginal baselines assign every entity the *same* risk, so their C-index
sits at ≈ 0.5 — the floor a real survival model must clear. Needs the `survival`
extra (`uv sync --extra survival`).

| Estimator | Params | Risk |
| --- | --- | --- |
| `…baselines.survival.KaplanMeierBaseline` | — | Population cumulative incidence `1 − S(t*)` (constant → C-index ≈ 0.5). |
| `…baselines.survival.NelsonAalenBaseline` | — | Population cumulative hazard `H(t*)` (constant). |
| `…baselines.survival.MarginalHazardBaseline` | — | Base rate — the marginal event fraction (constant). |
| `…baselines.survival.SingleFeatureCox` | `feature_index`, `alpha` | Cox proportional hazards on a **single** covariate — the survival analog of rank-by-one-feature. |

## Pinball / quantile loss

RMSE and MAE score a *point* forecast. A **quantile** forecaster
(`DummyRegressor(strategy=quantile)`, Croston, ETS intervals) is judged by
**pinball loss** at the quantile τ it targets:

```
pinball@τ = mean( τ·(y − ŷ)   when y ≥ ŷ,   (1 − τ)·(ŷ − y)   when y < ŷ )
```

It is a **loss** (lower is better) and computed in-Postgres
(migration 0020). The default regression metric set includes
`pinball@0.5`, `pinball@0.8`, `pinball@0.95` (median + two upper tails for
resource-prioritization); `pinball@0.5` is exactly half the MAE. Because
`triage.higher_is_better` knows `pinball@%` is a loss, audition ranks quantile
forecasters correctly with no extra wiring. Select a non-default set in the
`evaluation` block:

```yaml
evaluation:
  regression_metrics: [rmse, mae, r2, pinball@0.5, pinball@0.9]
```

## Does the ML earn its complexity?

The workflow is the same for every `problem_type`:

1. Put a baseline (or several) in the grid next to your real estimators.
2. Run once — baselines are cheap, so this barely moves wall-clock.
3. On the leaderboard, read the **gap**: real-model metric minus baseline metric.
4. A small gap means the features / model aren't earning their keep — simplify,
   or find better features, before shipping complexity.

### A worked example (DirtyDuck)

Running the classification and regression configs against the tutorial data gives
two opposite — and equally useful — verdicts:

```
classification (experiment.yaml)              regression (experiment-regression.yaml)
model                        prec@100  AUC     model            RMSE  pinball@0.95
BaselineRankMultiFeature        0.403  0.546   Ridge            4.325     1.358
ScaledLogisticRegression        0.388  0.584   DummyRegressor   4.462     1.675
SimpleThresholder               0.340  0.517   Persistence      5.332     1.585
RandomForestClassifier          0.338  0.568
DecisionTreeClassifier          0.335  0.561
DummyClassifier (floor)         0.260  0.500
```

Classification tells the subtler story. Every model clears the constant-prior
`DummyClassifier` (0.260 / 0.500), so the features carry signal — but the
DSSG-original `BaselineRankMultiFeature`, which does nothing but rank facilities
by their prior inspection count, **beats the ML on precision@100** (0.403 vs
0.388). The model only wins on **AUC** (0.584 vs 0.546): it ranks the whole list
better, not the operational top-k. That is exactly the humility these heuristic
floors are for. Regression's Ridge barely beats the constant floor (4.325 vs
4.462) and the time-series baselines are *worse* — the target's own history isn't
predictive here, so the ML earns very little. Note the quantile nuance: at the
0.95 tail Ridge (1.358) beats the `DummyRegressor` (1.675), even though the dummy
wins at the median.

Baselines are **opt-in** — you add them to `grid_config` deliberately (there is
no auto-injection). See the [configuration reference](/triage-pg/reference/configuration/)
for the `grid_config`, `target_history_lags`, `history_query`, and `evaluation`
keys, and [target history](/triage-pg/concepts/target-history/) for the
point-in-time contract behind the time-series floors.
