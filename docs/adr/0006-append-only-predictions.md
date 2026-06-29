# 0006. Predictions are append-only, timestamped, and time-partitioned

- Status: Accepted
- Date: 2026-06-04
- Status update (2026-06-28): Implemented — append-only `triage.predictions` with `scored_at` + `split_kind`, quarterly-partitioned (migration 0001); E2E asserts append-only.

The predictions table is **append-only**: every scoring run inserts rows carrying a `scored_at` wall-clock timestamp alongside `as_of_date`, and the table is time-partitioned. This is cheap insurance for the (deferred) production-monitoring use case — prediction history, drift, and trajectories become a `GROUP BY` later with zero migration — whereas a current-state table that overwrites on re-run can never recover history it never recorded.

## Considered alternatives
- *Current-state predictions keyed `(model, entity, as_of_date)`, overwritten on re-run* — rejected: simpler and smaller for batch experiments, but forecloses monitoring because history cannot be backfilled.

## Consequences
- Slightly larger tables and a concept to teach ("a score is not the latest score").
- Monitoring *features* (dashboards, drift math, `pg_cron`) are deferred, but the *data* that supports them is captured from day one.
