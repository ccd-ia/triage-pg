---
title: Target history & the leakage boundary
description: How the time-series baselines see each entity's own prior target values point-in-time-correctly — two shapes, two boundaries, one reserved-column contract.
sidebar:
  order: 5
---

The [time-series baselines](/triage-pg/reference/baselines/) (persistence, moving
average, seasonal-naive, Holt–Winters, Croston) forecast each entity's next
target from its **own prior target values**. But the design matrix is
cross-sectional — features plus *one* forward label per `(entity_id, as_of_date)`
— so that history isn't there. Exposing it is a genuine
[point-in-time](/triage-pg/concepts/point-in-time-correctness/) boundary: the
label-side twin of the fit-free/fit-based imputation split. Get it wrong and a
"floor" quietly sees the future, which is *worse* than no floor — it lowers the
bar deceptively.

The history is exposed in **two shapes**, under **one**
leakage boundary, both excluded from the feature set and from imputation.

## Shape 1 — windowed-label lags

The cheap baselines (persistence / moving-average / drift / promedio) read the
entity's **prior label values**: the same forward label, re-evaluated at earlier
as_of_dates. A label realized at date `t` over horizon `w` only becomes
*knowable* at `t + w`, so it is admissible as an input only where:

```
t + w ≤ as_of_date
```

That inequality **is** the leakage boundary. A label whose window has not closed
by `as_of_date` cannot appear in the lags — including the current row's own
label. The lags arrive as reserved matrix columns `_target_lag_1`
(most recent) … `_target_lag_N`, controlled by `target_history_lags: N`
(default 12). No extra config — they reuse the label you already defined.

Lag `k` is the entity's label at the `k`-th most recent label date of the
Experiment whose window has closed, the same dates for every entity. An entity
with no label at one of those dates (outside the cohort that date, say) has an
empty lag there; its older labels stay at their own lags. The spacing follows
the label dates, so a 6-month training frequency gives 6-month lags.

## Shape 2 — raw periodic series

Seasonal-naive, Holt–Winters/ETS, and Croston need an **evenly-spaced, complete**
series, with a value for every period back to the start of the data, not the
windowed label at the Experiment's label dates only. So the series gets its own
**optional `history_query`** — a period-level aggregation with its own
knowledge-date discipline:

```yaml
history_period: '6 month'          # one label window: the label sums six months
history_series_width: 8            # four years of six-month periods
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
   and e.date < {as_of_date}      -- the raw-series leakage boundary
  group by 1, 2
```

**One period is one label window.** These baselines forecast one
`history_period` ahead: seasonal naive returns the value one season back,
Holt–Winters and Croston their one-step forecast. That forecast is scored
against the label, so one period of the series must hold what the label holds,
over a window as long as the label's. DirtyDuck's label sums a facility's
violations over the six months after the as_of_date. The query above sums the
same violations over six-month periods that step back from the as_of_date, so
each value is the label the facility would have had at that period's start, and
a zero where it had no inspection. A season of 2 periods is then one year:
seasonal naive forecasts the same six months a year earlier. An earlier version
of this example counted inspections per month. Its seasonal-naive baseline
forecast the inspections of one month against six months of violations: on
average 4.9 violations too few, and below the outcome on 98% of the rows.

Only rows with `knowledge_date < as_of_date` may be selected. Each value is
placed on the calendar in reserved columns `_hist_0` (oldest) …
`_hist_{width-1}` (newest): slot `_hist_{width-k}` holds the period that starts
`k × history_period` before the as_of_date, for `history_series_width` slots
(default 24). A period the query does not return stays empty in its own slot;
nothing older moves into it. Slots before an entity's first period stay empty
(the series starts later). An empty slot after the first period is a **gap**:
with seasonal / Holt–Winters / Croston in the grid, a gap stops the run unless
`fill_missing_periods` is `zero` (gaps become 0) or `null` (those baselines make
no prediction for that row). That is why the query above returns a row for
every period, zeros included. `triage check history CONFIG` reports the gaps
before a run.

A raw-series baseline in the grid **without** a `history_query` is a hard error
at run start, and so is a `history_query` without a `history_period`. There is
no default period and no guess from the data: the period grid is a fact about
your data, and a guess is the silent assumption this layout exists to remove.

## Gaps, and what stops a run

Take one entity with no row for February and an as_of_date in June. Each value
sits at its own offset, so January is in the slot five months back and
February's slot is empty:

| slot (months back) | 5 | 4 | 3 | 2 | 1 |
| --- | --- | --- | --- | --- | --- |
| period | Jan | Feb | Mar | Apr | May |
| value | 5 | gap | 7 | 4 | 6 |

Up to 1.5.0 the series was packed by rank, so January slid into February's slot
and a read of "four months back" got a value from five months back. Seasonal
naive then read the wrong month, and Croston, which counts the periods between
demands, saw every interval shrink and forecast too high.

Two kinds of empty slot are treated differently:

| empty slot | example | what triage-pg does |
| --- | --- | --- |
| **leading**: older than the entity's first returned period | a facility that opened in March | stays empty in every mode; never an error |
| **interior**: between the first returned period and last month | a month with no row in the query result | a **gap**: decided by `fill_missing_periods` |

With `fill_missing_periods` unset, a gap stops the run while the matrix is
built, before featurizer runs and before any model is fitted, when the grid
holds a baseline that needs a complete calendar (seasonal naive, ETS,
Holt–Winters, Croston, Croston-SBA). The DirtyDuck regression example used a
`group by` over inspections up to 1.5.0, so a month with no inspection had no
row. Run on DirtyDuck with that query, the regression example fails like this:

```console
HistoryGapsError: 10778 of 11369 target-history series have missing periods
inside their history (for example entity 1 at as_of_date 2015-01-01: no row for
period 2014-02-01). Croston, HoltWinters, SeasonalNaive need a complete calendar
and would forecast from the wrong months. Fix one of two ways: return a row for
every period from the history_query, zeros included (a generate_series calendar
left-joined to the counts), or set fill_missing_periods: zero to treat a missing
period as zero. Run `triage check history EXPERIMENT.yaml` to list the gaps.
```

The first fix is the query above: a `generate_series` calendar left-joined to
the events returns a zero for every period without an inspection. The second,
`fill_missing_periods: zero`, writes 0 into interior gaps only. It never fills
slots before the first returned period, because there triage-pg cannot tell "no
demand" from "did not exist yet"; if you mean zeros there, return them from the
query. `fill_missing_periods: null` keeps the gaps: the complete-calendar
baselines make no prediction for a row with a gap (a WARNING gives the count,
and the row shows in the [coverage](/triage-pg/reference/evaluation/) of that
model group), and the lag-family baselines compute on the calendar positions
they have. The lags themselves never fail a run: a missing label means the
entity was outside the Cohort that date, not a zero.

`triage check history` runs the same placement without building anything. It
renders the `history_query` at the last test as_of_date (or at each
`--as-of DATE`) and counts the gaps. On the sparse query:

```console
$ triage check history experiment-regression-sparse.yaml
history_period '1 month', width 24, as_of_date(s) 2017-01-01

  check                               count
 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  series (entity × as_of_date)       14,537
  series with missing periods        14,320
  missing periods in all            208,804
  rows off the period grid                0
  rows at or after the as_of_date         0

 entities with the most missing
             periods

  entity_id   missing   present
 ───────────────────────────────
          7        23         1
         41        23         1
  …
Return a row for every period, zeros included, or set fill_missing_periods:
zero.
```

It exits 1 there, and 0 on `example/dirtyduck/experiment-regression.yaml`,
whose 18,909 series have no gap. A row whose period is not on the grid (the
15th of a month on a monthly grid) or not before the as_of_date is also
reported, and fails a run with `HistoryPeriodMisaligned`. The
[CLI reference](/triage-pg/reference/cli/#triage-check-history--gaps-in-the-target-history)
lists the options.

## Why reserved columns

Both shapes ride as **reserved** columns (`_target_lag_*`, `_hist_*`) that are:

- **excluded from the feature set** — so no *real* model can train on the label's
  own lags (they're enormously autocorrelated: "legal cheating" that would
  contaminate the floor-vs-model comparison);
- **excluded from imputation** — target history has *structural* missingness
  (cold-start entities, gaps); mean/median-imputing it would inject a wrong,
  leaky value. The baseline owns its own cold-start fallback instead.

The estimator seam feeds these columns (chronologically ordered) to any
estimator that declares `consumes_target_history` — at both fit and score time —
so the baseline sees the history and nothing else sees the label's lags.

## Configuration summary

| Key | Default | Applies to |
| --- | --- | --- |
| `target_history_lags` | 12 (when a lag baseline is present) | Shape 1 — how many `_target_lag_*` columns. |
| `history_query` | none | Shape 2 — required by seasonal / Holt–Winters / Croston. |
| `history_series_width` | 24 | Shape 2 — number of `_hist_*` slots. |
| `history_period` | none | Shape 2 — the length of one period; required with `history_query`. |
| `fill_missing_periods` | unset | Shape 2 — `zero` or `null` for gaps; unset stops the run on a gap. |

All five enter the matrix's content-addressed identity, so changing them
rebuilds the matrix rather than silently reusing a mismatched one. A matrix
with history also records its calendar layout in its identity, so no matrix
built by 1.5.0, whose history was packed by rank, is reused after an upgrade.
