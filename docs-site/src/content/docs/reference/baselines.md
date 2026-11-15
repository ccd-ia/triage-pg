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
`regression` scores it directly with **rmse / mae / r2 / wape /
[pinball@τ](#pinball--quantile-loss)**.
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
| `…baselines.timeseries.Persistence` | — | The most recent label that exists, however many label dates back. |
| `…baselines.timeseries.PromedioDisponible` | — | Mean of every label that exists. |
| `…baselines.timeseries.MovingAverage` | `window: [3, 6, 12]` | Mean of the labels that exist among the last `window` label dates. |
| `…baselines.timeseries.Drift` | — | Last value + the trend from the first to the last value, divided by their distance in label dates. |

**Raw-series family** — forecast the raw periodic series supplied by a
`history_query`, one `history_period` ahead. These **require** a
`history_query` (enforced at run start) and the exponential-smoothing pair
needs `statsmodels` (the `baselines` extra: `uv sync --extra baselines`). Their
forecast is a floor for the label only when one period of the series holds what
the label holds, over a window as long as the label's: with a six-month label,
a six-month `history_period` that sums the label's quantity
([target history](/triage-pg/concepts/target-history/#shape-2--raw-periodic-series)).

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
# violations summed over SIX-MONTH periods, the label's quantity over the label's
# window, point-in-time correct (only events known before {as_of_date}), one row per
# period with zeros included, so every value sits at its calendar offset.
target_history_lags: 6
history_series_width: 8
history_period: '6 month'
history_query: |
  with periods as (
    select f.entity_id, p::date as period
    from (select distinct entity_id from ontology.events) f
    cross join generate_series({as_of_date}::date - interval '6 month', date '2014-01-01',
                               interval '-6 month') p
  )
  select periods.entity_id, periods.period,
         sum(coalesce(jsonb_array_length(e.violations), 0))::double precision as value
  from periods
  left join ontology.events e
    on e.entity_id = periods.entity_id
   and periods.period <= e.date
   and e.date < periods.period + interval '6 month'
   and e.date < {as_of_date}
  group by 1, 2

grid_config:
  'sklearn.ensemble.RandomForestRegressor': { n_estimators: [100] }
  # lag floors (read _target_lag_*; no history_query needed)
  '…baselines.timeseries.Persistence': {}
  '…baselines.timeseries.MovingAverage': { window: [3, 6] }
  '…baselines.timeseries.Drift': {}
  # raw-series floors (need history_query above); 2 six-month periods = one year
  '…baselines.timeseries.SeasonalNaive': { season: [2] }
  '…baselines.timeseries.HoltWinters': {}
  '…baselines.timeseries.Croston': {}
```

### Layout and gaps

Both history shapes are placed on the calendar. Slot `_target_lag_k` holds the
entity's label at the k-th most recent label date of the Experiment whose
window has closed, and slot `_hist_{width−k}` holds the period that starts
k × `history_period` before the as_of_date. A slot with no value is empty, and
nothing older moves into it. Empty slots before an entity's first value mean
the series starts later; every baseline ignores them. An empty slot after the
first value is a **gap**, and each class declares what it does with one:

| Class | Reads | Needs a complete calendar | On an interior gap |
| --- | --- | --- | --- |
| `Persistence` | `_target_lag_*` | no | skips it: the newest slot with a value |
| `PromedioDisponible` | `_target_lag_*` | no | skips it: the mean of the slots with a value |
| `MovingAverage` | `_target_lag_*` | no | skips it inside the window, which never reaches past `window` slots; no prediction when the whole window is empty |
| `Drift` | `_target_lag_*` | no | divides the trend by the calendar distance between the first and last values |
| `SeasonalNaive` | `_hist_*` | yes | no prediction when the slot one season back is a gap; a series shorter than one season gives its last value |
| `ETS` | `_hist_*` | yes | no prediction |
| `HoltWinters` | `_hist_*` | yes | no prediction |
| `Croston` | `_hist_*` | yes | no prediction; a zero is a period with no demand |
| `CrostonSBA` | `_hist_*` | yes | no prediction; a zero is a period with no demand |

A gap in the lags is not an error: a missing label means the entity was not in
the Cohort that date. A gap in the raw series is. When a class that needs a
complete calendar is in the grid, the matrix build stops before any model is
fitted and gives the number of series with gaps, an example and the two fixes,
unless
`fill_missing_periods` says what a gap is: `zero` writes 0 into each gap (a
period with no events), `null` leaves it empty, and the five classes above
make no prediction for that row. `triage check history CONFIG` lists the gaps
before a run ([target history](/triage-pg/concepts/target-history/)).

**Cold start and coverage.** An entity with no usable history (e.g. the earliest
as_of_dates, before any label window has closed) can't be forecast — the
baseline **abstains** (Croston and SBA forecast 0 for an empty history
instead): it emits no prediction for that entity, so the metric is
computed over the rows it *could* score (a baseline never fabricates a future
value). A row with a gap under `fill_missing_periods: null` is the same case,
and the run logs a warning with the count. A baseline's `num_labeled` on the
leaderboard can therefore be below a real model's, and audition shows each
group's **coverage**, the share of the labeled rows it scored. To
compare groups on the rows all of them scored, use
`triage postmodel compare-rows` or `triage audition --common-rows`
([Evaluation](/triage-pg/reference/evaluation/)).

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

RMSE and MAE score a *point* forecast. A **quantile** forecaster, such as
`DummyRegressor(strategy=quantile)` or a gradient-boosting model with a
quantile loss, is judged by **pinball loss** at the quantile τ it targets:

```
pinball@τ = mean( τ·(y − ŷ)   when y ≥ ŷ,   (1 − τ)·(ŷ − y)   when y < ŷ )
```

It is a **loss** (lower is better) and computed in-Postgres. The default
regression metric set includes `pinball@0.5`, `pinball@0.8` and
`pinball@0.95` (the median and two upper tails for resource prioritization).
Each is computed only for a model that forecasts that quantile: training
records the quantile on the model (`DummyRegressor` with `strategy: median`
forecasts 0.5), and every other model gets the row with a NULL value. A point
forecast, and every time-series baseline here, is therefore never ranked on a
pinball loss. For the median dummy, `pinball@0.5` is exactly half its MAE.
Because `triage.higher_is_better` knows `pinball@%` is a loss, audition ranks
quantile forecasters correctly with no extra wiring. Select a non-default set
in the `evaluation` block:

```yaml
evaluation:
  regression_metrics: [rmse, mae, r2, pinball@0.5, pinball@0.9]
grid_config:
  'sklearn.dummy.DummyRegressor':
    strategy: ['quantile']
    quantile: [0.9]      # the trivial 0.9-quantile floor, scored by pinball@0.9
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
model                        prec@100  AUC     model                    RMSE    MAE
ScaledLogisticRegression        0.405  0.584   Ridge (alpha 1.0)       4.324  3.070
BaselineRankMultiFeature        0.376  0.546   DummyRegressor (mean)   4.396  3.132
DecisionTreeClassifier          0.369  0.561   DummyRegressor (median) 4.527  2.826
SimpleThresholder               0.364  0.517   Croston                 5.031  3.319
RandomForestClassifier          0.352  0.568   Persistence             5.332  3.433
DummyClassifier (floor)         0.295  0.500   SeasonalNaive (2)       5.826  3.996
```

Classification tells the subtler story. Every model clears the constant-prior
`DummyClassifier` (0.295 / 0.500), so the features carry signal — but the
DSSG-original `BaselineRankMultiFeature`, which does nothing but rank facilities
by their prior inspection count, **beats the tree and the forest on
precision@100** (0.376 vs 0.369 and 0.352). Only the logistic regression clears
it on both metrics (0.405 vs 0.376, AUC 0.584 vs 0.546). That is exactly the
humility these heuristic floors are for. (Precision@k ranks every facility and
counts the inspected ones in the cut; before v1.3.0 this table ranked
only inspected facilities and put the heuristic first.)

The regression column is the mean over the four test dates of a 1.6.0 run of
`experiment-regression.yaml`. The ridge regression has the lowest RMSE, but
only just below the constant mean (4.324 against 4.396), and the constant
median has the lowest MAE (2.826 against 3.070). The violation count has a long
right tail, and the median is the constant that minimizes absolute error, so
on MAE no model here beats a constant. The time-series baselines are *worse*:
a facility's own past counts predict its next six months less well than its
features do. Croston, the closest, smooths each facility's six-month violation
totals; seasonal naive repeats the same six months a year earlier, and forecasts
0 for a facility that was not inspected then. Read the lag baselines' numbers with their coverage. They scored
66.5% of the labeled rows against 82.5% for the regressors, so their means are
over different rows until `triage postmodel compare-rows` puts them on the
same ones.

Baselines are **opt-in** — you add them to `grid_config` deliberately (there is
no auto-injection). See the [configuration reference](/triage-pg/reference/configuration/)
for the `grid_config`, `target_history_lags`, `history_query`, and `evaluation`
keys, and [target history](/triage-pg/concepts/target-history/) for the
point-in-time contract behind the time-series floors.
