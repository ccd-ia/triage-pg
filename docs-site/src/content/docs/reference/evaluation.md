---
title: Evaluation — the per-as_of_date grain
description: A model is evaluated at every prediction time it was used for, not once per test window — what that grain buys, how to set the cadence, and how to select models on stability rather than a lucky mean.
sidebar:
  order: 4
---

Every model in triage-pg is evaluated at **every test `as_of_date` it scored** —
one row-set per prediction time in `triage.evaluations`, with an opt-in rollup
across the window in `triage.evaluations_windowed`. This page explains the
grain, the failure mode it replaces, how your `temporal_config` decides how many
evaluation points you get, and how to read the two surfaces that serve them.

## The grain, and the pooled metric it replaces

DSSG triage could not evaluate a model that was used at more than one prediction
time. When a test matrix spanned several `as_of_date`s (any `test_durations`
longer than `'0day'`), the whole window collapsed into one number: predictions
from every date were pooled into a single ranked list, so *precision@100 over a
month* meant "of the 100 highest-scoring `(entity, date)` pairs anywhere in that
month" — not "of the 100 we inspected each day". If you deploy daily at k=100
you actually inspect ~3,000 entities in a month; the pooled top-100 answers a
question no operator ever asks.

triage-pg fixes this structurally, at three layers:

- **Ranks partition by `(model_id, as_of_date)`** — each prediction time is its
  own ranked list.
- **Every in-PG metric function takes a required `as_of_date`** and filters on
  it; there is no pooled code path to fall into.
- **`as_of_date` is in the `triage.evaluations` primary key** — per-date rows
  are the storage grain, not a view over something coarser.

The behaviour is proven by `test_evaluation_is_per_as_of_date`
(`src/tests/adapter_tests/test_model_builder.py`), which asserts one evaluation
row-set per prediction time and `n_as_of_dates` reflecting the window.

The rollup view, `triage.evaluations_windowed`, aggregates the per-date rows per
`(model, split_kind, metric, parameter)`:

| Column | Meaning |
|---|---|
| `n_as_of_dates` | how many prediction times the model was evaluated at |
| `window_start` / `window_end` | first / last evaluated `as_of_date` |
| `value_mean` | mean of the per-date metric values |
| `value_min` / `value_max` | the worst / best date |
| `value_stddev` | spread across dates — the stability number |
| `num_labeled_total` / `num_positive_total` | summed label counts across dates |

The per-date rows are always the source of truth; the window is derived.

## How many evaluation points a model gets

`test_durations` × `test_as_of_date_frequencies` decides it, per split:

```yaml
# one point-in-time evaluation (the tutorials' teaching default):
test_durations: '0day'            # → 1 as_of_date per split → 1 evaluation point

# a deployed cadence — six months of weekly scoring:
test_durations: '6month'
test_as_of_date_frequencies: '1week'   # → ~26 as_of_dates → ~26 evaluation points
```

The committed tutorials use `'0day'` deliberately — one date keeps a first run
legible — but copied into a real project it silently hides this whole
capability: you get a single number per model and never see the variance.
`example/dirtyduck/experiment-cadence.yaml` is a runnable contrast against the
same baked data: a 6-month test window scored monthly — 6 evaluation points per
split (the window is half-open) — readable with
`triage leaderboard <hash> --windowed`.

**Treat `n_as_of_dates` as an assertion, not a statistic.** If you configured a
6-month window at weekly frequency and `n_as_of_dates` reads 1, the temporal
config is not doing what you think — check it before reading any metric off the
run.

## Selecting models: the mean is not the selector

With ~26 evaluation points per model, `value_mean` alone is a poor selection
criterion. A model at mean precision@100 = 0.38 with stddev 0.15 is
operationally **worse** than one at 0.35 with stddev 0.03: you get the bad weeks
too, and a program whose weekly precision swings 0.20→0.55 loses its users'
trust long before the mean recovers. Read `value_stddev` and `value_min`
alongside the mean — the steady 0.35 has a floor you can plan staffing around;
the lucky 0.38 does not.

**k must equal real capacity per cycle.** `precision@100_abs` at a weekly
cadence means "of the 100 we can actually inspect this week". Evaluating at a
monthly k while acting weekly is the old pooled-metric category error relocated
into your config — the number is computed correctly and answers nothing.

**One model per group and split.** Running an Experiment again with the same
features and grid fits a new model in each group, on the same splits, whenever
something else changed: the triage-pg version, a `history_query`, an
imputation strategy, the data. Audition, the leaderboard, the coverage and
common-rows commands and the group pages then read one model per group and
split, the one from the latest Run that built or reused it. A completed Run
outranks one still running or failed. The view `triage.current_models` lists
those models. The earlier ones stay in the database, and every command that
takes a model id still reads them.

## Top-k cuts the whole population, metrics count the labeled rows

A threshold metric (`precision@`, `recall@`, the bias audit, the dashboard's
threshold curve, the error tree) ranks **every scored row** at the
`as_of_date`, cuts the list at k, and only then looks at labels. An entity with
no label still takes a place in the top k. It is left out of the metric itself.
This is how DSSG triage computed them. Since v1.3.0,
triage-pg does the same.

| Parameter | k | precision@k | recall@k |
|---|---|---|---|
| `100_abs` | 100 | TP ÷ labeled rows among the 100 | TP ÷ labeled positives |
| `10_pct` | ⌈0.10 × scored rows⌉ | TP ÷ labeled rows in the cut | TP ÷ labeled positives |

The difference matters when labels are selective. In an inspection problem
(`task_framing: resource_prioritization`), a facility nobody inspected has no
label. The top 100 can hold 40 such facilities, and precision@100 then rests on
60 labels. `triage.evaluations.num_labeled_above_threshold` stores that
denominator, so read it next to the value. Precision is NULL when the cut holds
no labeled row.

Two deliberate differences from DSSG remain. Percentage thresholds round up
(`ceil`) where DSSG truncated, so `10_pct` can select one more row. The tie
bounds `value_worst`/`value_best` are the extremes over every ordering of the
rows tied at the cutoff, not a sort convention.

Rank-free metrics (AUC, average precision, Brier, log loss, ECE, the regression
metrics, the C-index) use the labeled rows only, as before.

The dashboard's threshold curve is one test date:
`triage.model_threshold_curve(model_id, as_of_date default null, subset_hash default '')`,
or the model's latest test date when `as_of_date` is NULL. It reads the same
population ranks, labels artifact and label horizon as `precision@`, so the
curve at k is the stored
`precision@k_abs` (since v1.3.1). The label horizon is the scored test
matrix's `label_timespan`, which need not equal the model's training horizon.

The third argument selects a subset, and `base_rate` gives the reference line. With the
default `''` the curve covers every scored row, and every other column is what
the two-argument function returned. A `subset_hash` makes the subset the
population, as it is for the subset metrics: the function ranks its members
among themselves, and the curve at k is the stored subset `precision@k_abs`.
`base_rate` is the population's labeled positives over its labeled rows, the
same value on every row. It is the precision a random ranking expects, and the
curve's own precision on its last row, where k is the whole population.
`triage model curve <id> [--date] [--subset]` prints the same rows
([CLI](/triage-pg/reference/cli/)), and
[Reading the threshold curve](/triage-pg/guides/reading-the-threshold-curve/)
explains how to read them.

The predicted list and the score histogram read their labels the same way,
through `triage.scored_labels(model_id, split_kind)` (since v1.3.2):
one row per latest score in that split, labeled from the scored matrix's labels
artifact. A date whose predictions have no matrix lineage falls back to the
training horizon and the run's labels artifact.

## The default metrics per problem type

Without an `evaluation` block, each `problem_type` gets its own set:

| `problem_type` | Default metrics |
|---|---|
| `classification` | `precision@`, `recall@` at `100_abs` and `10_pct`; `auc_roc`, `average_precision` |
| `regression` | `rmse`, `mae`, `r2`, `wape`, `pinball@0.5`, `pinball@0.8`, `pinball@0.95` |
| `regression_ranking` | the regression set, plus `lift@`, `captured@` and `ndcg@` at `100_abs` and `10_pct`, `aucc` and `spearman` |
| `survival` | `c_index` |

An `evaluation` block replaces the set for its Experiment
([Configuration](/triage-pg/reference/configuration/)). Audition defaults to
the metric the Experiment evaluated most, with ties broken by name, so a
regression_ranking Experiment on the default set auditions on `aucc`.

### Forecast errors

`rmse`, `mae` and `r2` score a point forecast over the labeled rows.
`wape` is the weighted absolute percentage error, Σ|ŷ − y| ÷ Σ|y|: the
absolute error as a share of the total outcome, so a WAPE of 0.6 means the
errors add up to 60% of what happened. It is NULL when Σ|y| is 0. Lower is
better for all three losses, and audition ranks them that way.

### pinball@τ only for a model that forecasts τ

`pinball@τ` is the loss a τ-quantile forecast minimizes:

```
pinball@τ = mean( τ·(y − ŷ) when y ≥ ŷ,  (1 − τ)·(ŷ − y) when y < ŷ )
```

A point forecast (a mean, a tree, a ridge regression) does not aim at any
quantile, so its pinball loss ranks it on a target it never had. Since 1.6.0,
`triage.models.forecast_quantile` records the quantile a model forecasts, and
`pinball@τ` is computed only where it equals τ. For every other model the row
is stored with its `num_labeled` and a NULL value, the way `ece@` is NULL for a
score outside [0, 1]; a heatmap then shows "not applicable" rather than a
missing cell.

Training reads the quantile from the estimator: an estimator that sets a
`forecast_quantile` attribute declares it, and the class name and parameters
are read for these:

| Estimator | Forecasts the quantile |
|---|---|
| `DummyRegressor` | `quantile` with `strategy: quantile`; 0.5 with `strategy: median` |
| `GradientBoostingRegressor`, `LGBMRegressor` | `alpha` with a quantile loss (default 0.9) |
| `HistGradientBoostingRegressor` | `quantile` with `loss: quantile` |
| `QuantileRegressor` | `quantile` (default 0.5) |

On the DirtyDuck regression Experiment, only the median `DummyRegressor`
gets a `pinball@0.5`, 1.413 averaged over the four test dates, half its MAE of
2.826. The ridge regressions, the trees and the time-series baselines get
NULL. Stored pinball values from before 1.6.0 change only when the model is
evaluated again; `triage check metrics` shows the difference.
`triage postmodel residuals MODEL --quantiles` prints, for a quantile model,
the share of rows at or below its forecast against τ.

### Allocation metrics for regression_ranking

A regression_ranking model ranks entities so a budget of k goes where the
outcome is largest. The forecast errors score the forecast, not that
allocation. Five metrics score the allocation:

| Metric | Value | Reads |
|---|---|---|
| `lift@k` | the mean outcome of the labeled rows in the top k over the mean outcome of every labeled row | the population ranking at the `precision@` thresholds |
| `captured@k` | the outcome in the top k over the total outcome | the same cut |
| `ndcg@k` | the order inside the top k: the sum of each labeled outcome over log2(rank + 1), over the same sum for the labeled outcomes sorted largest first | the same cut |
| `aucc` | the area under the cumulative-gains curve: x the share of rows selected, y the share of the outcome captured | the whole ranking |
| `spearman` | the rank correlation of score and outcome over the labeled rows, ties at their average rank | the labeled rows |

`lift@` and `captured@` cut the population ranking as `precision@` does: every
scored row is ranked, ties broken by `entity_id`, and the sums run over the
labeled rows in the cut. `num_labeled_above_threshold` holds those rows.
`aucc` treats a block of tied scores as one segment of the curve, the average
over every order of the block, so a constant score gives exactly 0.5 and a
random ranking about 0.5. All five are higher is better. `lift`, `captured`,
`ndcg` and `aucc` are shares of a total, so they are NULL when a labeled outcome is
negative or the total is 0; `spearman` is NULL when the score or the outcome
is constant. The rows are written with their `num_labeled` either way.

`lift@` and `captured@` treat the top k as a set, so two models that select
the same k rows score the same whatever their order. `ndcg@` scores the order:
with k = 3 and outcomes 10, 5 and 1, the order 10, 5, 1 scores 1.00 and the
order 1, 5, 10 scores 0.67. Use it when the list is worked from the top and
the work may stop before k, such as inspectors who visit in rank order until
the week ends. The gain is the outcome itself, not 2^outcome - 1, which
overflows for counts and amounts. An unlabeled row keeps its position in the
ranking and adds no gain. On a classifier the gain is the 0/1 label; list
`ndcg@` in a classification Experiment's `metrics` to store it there.
`triage check metrics` recomputes it with scikit-learn's `ndcg_score`.

`triage.gains_curve(model_id, split_kind, as_of_date, points)` returns the
curve itself, from k = 0 to the whole population, with the share captured, the
lift and the mean outcome in the top k. `triage plot gains MODEL` draws it.

## Coverage, and comparing on common rows

A time-series baseline with no history for a row makes no prediction for it,
so the row is missing from that model's evaluation. Each model group is then
evaluated on its own rows, and a group that skipped the hard rows can rank
above one that scored them all.

**Coverage** is the share of a date's labeled rows that a model scored. The
audition table, the model-groups table and `triage audition` show each group's
mean coverage over its test dates. On the DirtyDuck regression Experiment
the regressors cover 82.5% of the labeled rows and the lag baselines 66.5%.

Two commands put groups on the same rows, the labeled rows every listed group
scored on that date:

- `triage postmodel compare-rows <hash>` reads only. Per date and group it
  prints the labeled, scored and common row counts, the MAE on the group's own
  rows, and MAE, RMSE, WAPE and bias on the common rows.
- `triage audition <hash> --common-rows` registers the common rows as a Subset
  named `common:<md5>`, evaluates every model on it, and selects on it. The
  hash covers the sorted group ids, so another set of groups makes another
  Subset and never changes an earlier one.

Without `--common-rows`, audition selects as it always did. The
[CLI tour](/triage-pg/reference/cli/) shows both on DirtyDuck.

## The metric audit

`triage check metrics --experiment <hash>` (or `--model`, `--group`)
recomputes every stored evaluation with scikit-learn, numpy or
scikit-survival over the rows the SQL function read, and reports the largest
gap per metric. On the three DirtyDuck tutorial Experiments the largest gap
over 7,296 stored values is 2.21e-14, against a tolerance of 1e-9.

## Two surfaces, two grains

The CLI serves both grains, and they answer different questions:

| Surface | Reads | Grain | Question it answers |
|---|---|---|---|
| `triage leaderboard <hash>` | `triage.leaderboard` (matview) | per `as_of_date` | how did each model do at each prediction time? |
| `triage leaderboard <hash> --windowed` | `triage.evaluations_windowed` | one row per model | is this model *steady* across its window? |
| `triage audition <hash>` | `triage.evaluations_windowed` (via `triage.audition`) | windowed | which model *group* should we pick? |

`--windowed` adds `n_as_of_dates`, `value_mean`, `value_stddev`, `value_min` to
the leaderboard output and says so in its header — a windowed mean must never be
mistaken for a single date's value. Without the flag, the output is unchanged
from what it always was.

## Label maturity at the window's edge

A fine test cadence raises an obvious worry: with a 6-month `label_timespan`,
aren't the last six months of test dates unlabelable? **By construction, no** —
timechop walks the last test date back from `label_end_time` by the test
`label_timespan`, and back again by `test_durations`, before generating any
dates. Every test `as_of_date` satisfies
`as_of_date + label_timespan ≤ label_end_time`.

The residual risk is a **data** problem the config cannot see: if
`label_end_time` claims a horizon your loaded data does not actually reach,
late dates have labels that *should* exist but don't. The failure is quiet.
Those entities still take places in the top k, but the metric counts only
labeled entities, so precision rests on fewer labels and recall on fewer
positives. The tells are `num_labeled` and `num_labeled_above_threshold`: a late
date where either drops off a cliff is immature data, not a bad model. Don't
average blindly across dates without looking at them.

## See also

- [Configuration reference](/triage-pg/reference/configuration/) — the
  `temporal_config` keys themselves.
- [Problem types](/triage-pg/reference/problems/) — what the score means per
  `problem_type`; the metric catalog.
- [Concepts: point-in-time correctness](/triage-pg/concepts/point-in-time-correctness/)
  — why every boundary in this page is strict.
