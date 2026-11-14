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

Seasonal-naive, Holt–Winters/ETS, and Croston need a **fine, evenly-spaced**
series (e.g. monthly counts), not the coarse windowed label at as_of_date
cadence. That is a genuinely different quantity, so it gets its own **optional
`history_query`** — a period-level aggregation with its own knowledge-date
discipline:

```yaml
history_period: '1 month'
history_query: |
  with months as (
    select f.entity_id, m::date as period
    from (select distinct entity_id from ontology.events) f
    cross join generate_series(date '2014-01-01', {as_of_date}::date - interval '1 month',
                               interval '1 month') m
  )
  select months.entity_id, months.period, count(e.entity_id) as value
  from months
  left join ontology.events e
    on e.entity_id = months.entity_id
   and date_trunc('month', e.date)::date = months.period
   and e.date < {as_of_date}      -- the raw-series leakage boundary
  group by 1, 2
```

Only rows with `knowledge_date < as_of_date` may be selected. Each value is
placed on the calendar in reserved columns `_hist_0` (oldest) …
`_hist_{width-1}` (newest): slot `_hist_{width-k}` holds the period that starts
`k × history_period` before the as_of_date, for `history_series_width` slots
(default 24). A month the query does not return stays empty in its own slot;
nothing older moves into it. Slots before an entity's first period stay empty
(the series starts later). An empty slot after the first period is a **gap**:
with seasonal / Holt–Winters / Croston in the grid, a gap stops the run unless
`fill_missing_periods` is `zero` (gaps become 0) or `null` (those baselines make
no prediction for that row). That is why the query above returns a row for
every month, zeros included. `triage check history CONFIG` reports the gaps
before a run.

A raw-series baseline in the grid **without** a `history_query` is a hard error
at run start, and so is a `history_query` without a `history_period`.

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
rebuilds the matrix rather than silently reusing a mismatched one.
