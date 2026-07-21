---
title: Target history & the leakage boundary
description: How the time-series baselines see each entity's own prior target values point-in-time-correctly — two shapes, two boundaries, one reserved-column contract (ADR-0030).
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

This is **ADR-0030**. The history is exposed in **two shapes**, under **one**
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

## Shape 2 — raw periodic series

Seasonal-naive, Holt–Winters/ETS, and Croston need a **fine, evenly-spaced**
series (e.g. monthly counts), not the coarse windowed label at as_of_date
cadence. That is a genuinely different quantity, so it gets its own **optional
`history_query`** — a period-level aggregation with its own knowledge-date
discipline:

```yaml
history_query: |
  select entity_id, date_trunc('month', date)::date as period, count(*) as value
  from ontology.events
  where date < {as_of_date}      -- the raw-series leakage boundary
  group by 1, 2
```

Only rows with `knowledge_date < as_of_date` may be selected. The series is
pivoted into reserved columns `_hist_0` (oldest) … `_hist_{width-1}` (newest),
left-padded for short histories, capped by `history_series_width` (default 24).
A raw-series baseline in the grid **without** a `history_query` is a hard error
at run start.

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
| `history_series_width` | 24 | Shape 2 — max `_hist_*` width. |

All three enter the matrix's content-addressed identity, so changing them
rebuilds the matrix rather than silently reusing a mismatched one. The decision
and its trade-offs are recorded in
[ADR-0030](https://github.com/ccd-ia/triage-pg/blob/main/docs/adr/0030-target-history-point-in-time-path.md).
