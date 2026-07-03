# 0027. Monitoring: external scheduler → CLI; drift as SQL over append-only predictions

- Status: Accepted
- Date: 2026-07-03
- Deciders: Adolfo (scope), Claude (recommendation per the v1-completion plan)
- Status update (2026-07-03): Implemented — `triage score` (the monitoring-facing forward-scoring
  entrypoint over `adapters/forward.py`); migration 0012 (`monitoring_volume`,
  `monitoring_outcome_tracking`, `monitoring_score_drift` PSI+KS, `monitoring_calibration`);
  dashboard Monitoring view; `infra/terraform/monitoring.tf` EventBridge template (gated with
  the cloud apply); PSI/KS cross-checked against scipy/hand-computed fixtures
  (`src/tests/test_monitoring_views.py`); `docs/monitoring.md` is the operator guide.

ADR-0006 made predictions append-only, timestamped, and partitioned precisely so monitoring
would be "a GROUP BY later". This ADR is that later. (Three-criteria check: *hard to reverse* —
a scheduling daemon or an external drift service would be new operational surface that is
expensive to walk back; *surprising without context* — "where is the scheduler?" deserves a
recorded answer; *real trade-off* — daemon vs pg_cron vs external scheduler are all defensible.)

## Decision

**1. No new daemon; scheduling belongs to the operator's scheduler.** Recurring forward
scoring is the operator's cron (local) or an EventBridge Scheduler rule submitting the Batch
job (cloud) invoking the existing CLI — `triage score <model_id> <prediction_date>`
(the monitoring-facing name for the forward-scoring path; `predictlist` remains as the
inherited alias). The headless-complete core (ADR-0012) stays a set of invocable commands;
re-invocation is safe because predictions append (ADR-0006) and provenance is already recorded
(`runs.purpose='forward_score'` + `prediction_date`, ADR-0018).

**2. Drift and health are SQL over `triage.predictions` (ADR-0007 consistency), migration 0012:**
- `monitoring_volume` (view) — predictions + distinct entities per (model group, model,
  split_kind, scored-on day): the heartbeat that scoring is running and cohort sizes are stable.
- `monitoring_score_drift(model_group, reference_from/to, window_from/to)` (function) —
  **PSI** over reference-decile bins (ε-smoothed) + **KS** between the two score samples.
  Windows are parameters, so any policy is queryable.
- `monitoring_calibration(model_id, split_kind, as_of_date, label_timespan)` (function) —
  score-decile vs realized outcome rate, over the artifact-pinned `labeled_ranks` (0011).
- `monitoring_outcome_tracking` (view) — realized metrics over time: `triage.evaluations` is
  already idempotent-upsert per (model, date, metric), so when labels arrive for a scored
  date, re-running `evaluate_model` writes the realized row; this view sequences them per
  model group with the run's `purpose`.

**3. The reference window is pinned, not rolling.** Drift is always "vs what we validated":
the operator records the deployed group's reference window (typically its validation period)
and passes it to `monitoring_score_drift`; a rolling reference hides slow drift. The function
takes both windows as parameters precisely so the pinned policy is a convention, not a schema.

## Considered alternatives

- *A triage scheduler daemon* — rejected: a long-running process is new operational surface
  (supervision, upgrades, credentials at rest) against the headless ethos; cron/EventBridge
  already exist and are the operator's own audited tooling.
- *pg_cron inside the database* — rejected: forward scoring needs Python (featurizer, the
  estimator); pg_cron's carve-out is in-database housekeeping only.
- *An external drift service (Evidently, whylogs)* — rejected: the metrics are group-bys over
  data already in PG; a second engine violates the one-database ethos (ADR-0003) for math a
  view expresses.

## Consequences

- Monitoring cost stays where ADR-0006 put it: partitioned scans bounded by `scored_at`.
- Alerting is out of scope for v1 (recorded): the views are the query surface an alerting
  layer (cron + psql exit code, Grafana, CloudWatch) can poll.
- Feature drift (matrices are Parquet, off-DB) is future work — score/outcome drift lands
  first because it lives where the data lives.
