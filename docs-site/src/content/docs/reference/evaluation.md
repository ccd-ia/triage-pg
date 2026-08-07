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
late dates have labels that *should* exist but don't. The failure is safe but
silent — the metric functions count only labeled entities (`outcome is not
null`), so those dates produce fewer or no evaluation rows rather than wrong
ones. The tell is `num_labeled`: a late date whose `num_labeled` drops off a
cliff is immature data, not a bad model. Don't average blindly across dates
without looking at it.

## See also

- [Configuration reference](/triage-pg/reference/configuration/) — the
  `temporal_config` keys themselves.
- [Problem types](/triage-pg/reference/problems/) — what the score means per
  `problem_type`; the metric catalog.
- [Concepts: point-in-time correctness](/triage-pg/concepts/point-in-time-correctness/)
  — why every boundary in this page is strict.
