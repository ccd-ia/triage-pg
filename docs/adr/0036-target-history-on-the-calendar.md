# 0036. Target history is placed on the calendar

- Status: Accepted
- Date: 2026-11-14
- Deciders: Adolfo De Unánue

## Context

meio ran a demand-forecasting pilot on triage-pg 1.5.0, a regression on monthly demand per node and SKU. In 1.5.0, `pivot_series_to_history_columns` ranked the periods each entity had and packed them to the right, so `_hist_k` held the k-th most recent period that existed. It did not hold the period k steps before the as_of_date. A month with no row shifted older months into its slot. SeasonalNaive's "12 months back" then read a month further back, and Croston counted intervals by array position, so with zero months absent every interval shrank toward 1 and the forecast was biased upward. In meio's December 2025 test matrix, 34,184 of 67,906 pairs had fewer periods than the calendar window (meio's report).

The windowed-label lags had the same flaw. They used `row_number()` over each entity's own prior labels, so an entity missing a label date got the older label in `_target_lag_1`.

ADR-0030 never said the series must be complete, and nothing checked it. The DirtyDuck example's own `history_query` was a group by over events, so months without inspections were missing (14,320 series with gaps, per the fix commit 028c60f8).

## Decision

ADR-0030's two shapes stay. Both are now placed by calendar offset.

- **Raw series.** `history_period` (for example `'1 month'`) is a required key whenever `history_query` is set. It has no default and no inference, because a default, or a period inferred from the smallest gap, would bring back the silent assumption. Slot `_hist_{width-k}` holds the period that starts k x `history_period` before the as_of_date, and a missing period is NULL. A row inside the window that is off the grid, or at or after the as_of_date, raises `HistoryPeriodMisaligned`.
- **Gaps.** Leading NULLs (a series that starts late, such as a new SKU or facility) are allowed. Interior NULLs, between the entity's oldest returned period and offset 1, are gaps.
- **`fill_missing_periods`.** `zero` fills interior gaps only. It never fills before the oldest returned period, because there triage-pg cannot tell "no demand" from "did not exist". `null` leaves them NULL. Unset means strict: the matrix build raises `HistoryGapsError` before featurizer runs when a series has interior gaps and the grid holds a baseline with `requires_complete_calendar = True`. The message gives the counts, an example, the baselines affected and the two fixes.
- **Baselines.** Each time-series baseline declares `requires_complete_calendar` and `gap_policy`. SeasonalNaive, ETS, HoltWinters, Croston and CrostonSBA need a complete calendar and use `abstain`. That policy is reached only with `fill_missing_periods: null`, where an abstention is a non-finite score, logged at WARNING with a count and shown in the coverage column. Persistence, PromedioDisponible, MovingAverage and Drift use `calendar_positions`.
- **Lags.** Lag k is the entity's label at the k-th admissible label date of the Experiment's grid, the same grid for every entity. ADR-0030's predicate `lab.as_of_date + lab.label_timespan <= cur.as_of_date` is kept word for word. A missing label is NULL and not an error, because it means the entity was outside the cohort on that date.
- **Identity.** Matrices with history carry `history_layout: calendar`, `history_period` and `fill_missing_periods` in their identity, so no rank-packed matrix is reused, including through the `logical_id` fallback. Matrices without history hash as before. Gap counts are stored in matrix metadata, so a reused matrix is checked against the grid of the run that reuses it.
- **`triage check history CONFIG [--as-of DATE] [--json PATH]`.** It renders the `history_query`, places it on the grid and reports gaps and off-grid rows. Exit 0 is clean, 1 is gaps or off-grid rows, 2 is bad input. It builds and writes nothing.

### Alternatives considered

- **Keep rank packing and document that the query must be complete.** Rejected: nothing would check it, and the failure is silent wrong forecasts.
- **Fill gaps with zero by default.** Rejected: it turns "outside the cohort" or "did not exist yet" into "zero demand" without the user saying so.
- **A default `history_period` of 1 month, or inference from the data.** Rejected for the reason given under Decision.
- **Raise inside `predict` on a gap.** Rejected: a few entities would kill a whole grid. Under `null` a baseline abstains on those entities instead.

## Consequences

- This is a breaking change for every config with a `history_query`, meio's included. It must add `history_period`, and a sparse query fails until it returns every period or sets `fill_missing_periods`.
- SeasonalNaive, Croston and the moving averages change on any entity with gaps, and lags change where an entity misses a label date. Numbers differ from 1.5.0.
- Matrix identity changes, so no matrix built before this ADR is reused.
- The DirtyDuck regression example's `history_query` now uses a `generate_series` calendar left-joined to the counts.