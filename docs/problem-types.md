# triage-pg — `problem_type` and the ranking spine

> triage-pg is a **prioritization** system. Its whole architecture is one spine — **produce a score
> → rank entities → evaluate the ranking** — and the `problem_type` on an Experiment selects a few
> swaps on that spine. Pick the wrong `problem_type` and you're answering a different question.

- Source of truth: [ADR-0010](adr/0010-problem-type-ranking-spine-survival-ready-labels.md)
  (problem_type switch on a ranking spine; survival-ready labels),
  [ADR-0007](adr/0007-in-postgres-evaluation-and-sql-bias-metrics.md) (in-PG metrics, organized by problem_type).
- `problem_type` is part of the **Experiment identity** (see [experiment-and-run.md](experiment-and-run.md)):
  switching it is a *different problem*, not a new Run.

## 1. The spine

Every problem type rides the same three steps. Only the **how** of each step changes.

```mermaid
flowchart LR
  S["① score<br/>model emits a number per entity"] --> R["② rank<br/>order entities by that score"]
  R --> E["③ evaluate<br/>metric over the ranking"]
  classDef s fill:#eef,stroke:#557;
  class S,R,E s;
```

| `problem_type` | ① score is… | ② rank by… | ③ primary metrics | label columns |
|---|---|---|---|---|
| `classification` | `P(y=1)` | descending probability | AUC-ROC, precision@k, recall@k, AP | `outcome` (0/1) |
| `regression_ranking` | predicted value | descending predicted value | precision@k + RMSE/MAE/R² | `outcome` (continuous) |
| `regression` (pure) | predicted value | (ranking incidental) | RMSE, MAE, R² | `outcome` (continuous) |
| `survival` *(future)* | predicted risk / hazard | descending risk | C-index, Brier *(deferred)* | `duration`, `event_observed` |

**Why a ranking spine at all?** Public-policy ML is almost always *"we can act on the top *k* —
which entities?"* — slow 311 requests to escalate, facilities to inspect, students to support. The
deliverable is an **ordering**, so the spine optimizes and measures the ordering directly.
precision@k / recall@k are first-class, not afterthoughts.

## 2. The three live modes

### classification
The default. The label query emits an integer `outcome` (0/1); the model emits `P(y=1)`; entities
are ranked by that probability and scored with AUC / precision@k / recall@k / average-precision.
All three tutorial datasets (DirtyDuck inspections, DonorsChoose funding, Chicago 311 resolution)
are classification.

```yaml
problem_type: classification
label_config:
  query: |
    select entity_id, (… )::int as outcome   # 0/1
    from … where {as_of_date} <= date and date < {as_of_date}::date + {label_timespan}
```

### regression_ranking — the primary mode for continuous targets
When the target is a continuous quantity (dollars at risk, days-to-resolution, demand units) but the
**decision is still "act on the top *k*"**, rank by the predicted value. You get precision@k *on the
continuous target* **plus** the regression error metrics — the best of both: a usable priority list
and a calibrated sense of magnitude. Prefer this over pure regression whenever the output drives a
prioritized action.

```yaml
problem_type: regression_ranking
label_config:
  query: |
    select entity_id, (…)::numeric as outcome   # continuous
    from … where {as_of_date} <= date and date < {as_of_date}::date + {label_timespan}
```

### regression (pure)
When you genuinely care about the **point estimate** and not an ordering (forecasting a total, an
expected cost), use pure regression: RMSE / MAE / R² only, ranking incidental.

> See `example/dirtyduck/greenfield-regression.yaml` for a `regression_ranking` config on the food DB.

## 3. Survival — ready in the schema, deferred in the build

The greenfield label schema is **survival-ready now**: alongside `outcome` it carries optional,
nullable `(duration, event_observed)` columns. So a `survival` `problem_type` (rank by predicted
risk/hazard; evaluate with the C-index — itself a ranking metric, so it lands on the same spine) can
be added later **without a schema migration**.

```sql
-- triage.labels carries all three; classification/regression leave the survival pair NULL
label_hash, entity_id, as_of_date, label_timespan,
  outcome,                 -- classification / regression
  duration, event_observed -- survival (nullable elsewhere)
```

This is deliberate cheap insurance: survival-as-ranking (time-to-recidivism, time-to-eviction, …) is
squarely in the target domain, and binary triage would have *discarded* the censoring information
("no event in the window" = 0) that survival needs. What remains for the future survival build is the
*implementation*, not the schema: censoring-aware label generation, survival estimators, and the
C-index / Brier metrics.

## 4. Choosing — a 10-second guide

```mermaid
flowchart TD
  Q{"What is the target?"}
  Q -->|"binary yes/no event"| C["classification"]
  Q -->|"continuous quantity"| Q2{"Is the decision<br/>'act on the top k'?"}
  Q -->|"time until an event<br/>(possibly censored)"| SV["survival (future)"]
  Q2 -->|yes| RR["regression_ranking<br/>(primary continuous mode)"]
  Q2 -->|"no — I need the point estimate"| RG["regression (pure)"]
```

| If you… | use |
|---|---|
| predict a yes/no outcome and rank by probability | `classification` |
| predict a continuous value but still act on a top-*k* list | `regression_ranking` |
| only need an accurate point estimate, no prioritization | `regression` |
| predict time-to-event with censoring | `survival` *(schema ready, build deferred)* |
