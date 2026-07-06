# Monitoring — scheduled forward scoring + drift over append-only predictions

![The Monitoring view: score-volume heartbeat, PSI/KS drift chips vs the pinned reference, realized outcomes](images/monitoring-view.png)

ADR-0006 made every scoring run an append-only, `scored_at`-timestamped insert precisely so
production monitoring would be "a GROUP BY later". ADR-0027 lands that later: **no daemon** —
the operator's scheduler invokes the existing CLI, and drift/health are SQL (migration 0012).

## Scoring on a schedule

The monitoring entrypoint is `triage score <model_id> [prediction_date]` (forward scoring —
`runs.purpose='forward_score'`, ADR-0018). The date defaults to **today**, so a schedule line
needs no date arithmetic; re-invocation is safe (appends, never overwrites).

**Local (cron):**

```cron
# score model 42 against fresh data on the 1st of each month, 06:00
0 6 1 * *  cd /path/to/project && DATABASE_URL=… uv run triage score 42 --project-path /data/artifacts
```

**Cloud (EventBridge → Batch, gated with the cloud apply):** populate
`forward_score_schedules` in `infra/terraform/monitoring.tf` — one EventBridge Scheduler rule
per deployed model, submitting the standard job definition with the command overridden to
`triage score <model_id>` and the project's `TRIAGE_RDS_DB`/`TRIAGE_RDS_USER` injected.

## Realized outcomes: re-evaluate when labels arrive

`triage.evaluations` upserts idempotently per `(model, split, date, metric, parameter)` —
when the label window for a scored date closes (outcomes became knowable), re-run the
evaluation and the REALIZED metric lands next to the historical ones:

```sql
select triage.evaluate_model(42, 'production', date '2026-06-01', interval '14 days',
                             '{"metrics": ["precision@"], "thresholds": ["100_abs"]}');
```

`triage.monitoring_outcome_tracking` sequences those rows per model group (tagged with each
run's `purpose`), which is the realized-vs-expected line the dashboard draws.

## Drift

**The reference window is pinned, not rolling** (ADR-0027): drift is always "versus what we
validated". Record the deployed group's reference window (typically its validation period)
and compare each new scoring window against it:

```sql
-- PSI (reference-decile bins, ε-smoothed) + KS between the two score samples:
select * from triage.monitoring_score_drift(
    7,                                        -- model_group_id
    '2026-01-01', '2026-02-01',               -- pinned reference window (scored_at)
    '2026-06-01', '2026-07-01');              -- the window under inspection
```

Rules of thumb: PSI < 0.10 stable · 0.10–0.25 investigate · > 0.25 the population has moved.
KS is the distribution-free companion (max ECDF gap; scipy-compatible semantics).

## Health & calibration

```sql
-- the scoring heartbeat: rows + distinct entities per (group, model, split, day)
select * from triage.monitoring_volume where model_group_id = 7 order by scored_on;

-- score-decile vs realized outcome rate at one evaluated date (labels artifact-pinned):
select * from triage.monitoring_calibration(42, 'test', date '2026-06-01', interval '14 days');
```

The dashboard's **Monitoring** view renders volume, drift, and the realized-metric series per
model group; everything it shows is exactly these views/functions (ADR-0012 — no UI-only logic).

## Scope notes

- **Alerting** is deliberately out of v1: these views are the query surface; point cron+psql,
  Grafana, or CloudWatch at them.
- **Feature drift** is future work — matrices live on Parquet/S3 (off-DB); score + outcome
  drift land first because they live where the data lives.
