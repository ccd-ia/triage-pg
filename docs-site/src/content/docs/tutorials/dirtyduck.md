---
title: "DirtyDuck — the full case study"
description: Chicago food inspections, end to end — the same data formulated as resource prioritization and as an early warning system, and why that one decision changes everything.
sidebar:
  order: 2
  label: DirtyDuck (full case)
---

This is triage-pg's version of DSSG triage's
[Dirty Duck tutorial](https://dssg.github.io/triage/dirtyduck/): the same
Chicago food-inspections story, told on the greenfield stack. Its centerpiece
is the lesson DSSG structured its whole tutorial around — the **same data
supports two genuinely different prediction problems**, and the difference
lives in a single modeling decision.

Run the [Dirty Duckling smoke test](/triage-pg/tutorials/dirtyduckling/) first;
this page assumes your stack works (food DB up on 5440, schema migrated).

## The case

Chicago inspects food establishments — restaurants, groceries, schools,
bakeries. Some inspections find critical violations ("fail"); most don't.
Inspectors are scarce: only about half the active facilities get inspected in
any six-month window. Two different city teams could ask two different
questions of the same inspection history:

1. **The inspections team**: *"Given we can only visit so many facilities,
   which ones — if inspected — are most likely to be found in violation?"*
2. **A monitoring/early-warning team**: *"Which facilities will show up on
   the failed-inspections register in the next six months?"*

These sound alike. They are not — and the difference is exactly what the
`task_framing` chip in the dashboard makes visible. (The full taxonomy of
both axes — problem types and observation regimes — is the
[problem-space reference](/triage-pg/reference/problems/).)

## The data

`just tutorial-up` gives you a PostgreSQL with three layers (the pattern every
triage-pg project follows):

- `raw.*` — the inspections file as ingested;
- `clean.*` — typed, deduplicated;
- `ontology.*` — the modeling layer: **`ontology.entities`** (one row per
  facility: type, zip, an `activity_period` daterange) and
  **`ontology.events`** (one row per inspection: `date`, `result`, `risk`,
  `type`).

One column deserves ceremony: `ontology.events.date` is the inspection's
**knowledge date** — when the outcome became known. Every feature computed
from events is joined *as of* a date using this column, never anything later.
That is the cardinal rule of temporal ML: **features for an `as_of_date` may
use only what was knowable strictly before it.** Get this wrong and your
backtest quietly reads the future ("leakage"); every number it reports becomes
fiction.

## The problem, formulated twice

Both formulations share the cohort — active facilities at each `as_of_date`:

```sql
select e.entity_id
from ontology.entities as e
where e.activity_period @> {as_of_date}::date
```

and the temporal frame: labels observed over 6-month windows, a model retrained
every 6 months, four test splits (2015-07 → 2017-01). The `{as_of_date}` and
`{label_timespan}` placeholders are filled by the temporal engine — you write
the SQL once, it runs point-in-time-correctly for every split.

### Formulation 1 — resource prioritization (the committed config)

[`example/dirtyduck/experiment.yaml`](https://github.com/ccd-ia/triage-pg/blob/main/example/dirtyduck/experiment.yaml)
labels a facility from its inspections **in the window**:

```sql
select entity_id,
       bool_or(result = 'fail')::integer as outcome
from ontology.events
where {as_of_date}::date <= date
  and date < {as_of_date}::date + {label_timespan}
group by entity_id
```

A facility with no inspection in the window returns **no row — its label is
NULL**. Not zero: *unknown*. We didn't look. That is the
**resource-prioritization regime** (`task_framing: resource_prioritization`
in the config), and it shows up everywhere downstream:

- **~54% labeled** — the %-labeled card carries the "selective labels —
  <100% expected" note instead of an alarm;
- **base rate 0.277** — *among inspected facilities*, 28% fail;
- training and evaluation use only labeled rows, so the model learns
  "conditional on being the kind of place that gets inspected…" — with all
  the selection bias that implies. (Inspections aren't random: complaints,
  risk schedules, and history drive who gets visited.)

### Formulation 2 — early warning (the EIS twin)

[`example/dirtyduck/experiment-eis.yaml`](https://github.com/ccd-ia/triage-pg/blob/main/example/dirtyduck/experiment-eis.yaml)
changes exactly one thing — what "no inspection" means:

```sql
select e.entity_id,
       coalesce(bool_or(ev.result = 'fail'), false)::integer as outcome
from ontology.entities as e
left join ontology.events as ev
  on ev.entity_id = e.entity_id
 and {as_of_date}::date <= ev.date
 and ev.date < {as_of_date}::date + {label_timespan}
where e.activity_period @> {as_of_date}::date
group by e.entity_id
```

"Will this facility appear on the failed-inspections register?" is knowable
for **every** active facility — the register is complete — so no-event
coalesces to **0** and the label covers the whole cohort
(`task_framing: early_warning`).

Because the label SQL genuinely changed, this is a **different experiment**:
triage-pg hashes the problem (cohort + label + temporal config) and the two
configs get different hashes. Run both and compare:

```bash
uv run triage --dbfile dirtyduck-database.yaml run \
  example/dirtyduck/experiment.yaml --project-path /tmp/dirtyduck-run
uv run triage --dbfile dirtyduck-database.yaml run \
  example/dirtyduck/experiment-eis.yaml --project-path /tmp/dirtyduck-run
```

| | resource prioritization | early warning |
| --- | --- | --- |
| experiment hash | `b9e38fd8f366…` | `c0d16446f567…` |
| % labeled | **53.7%** | **100%** |
| base rate | **0.277** | **0.116** |
| the model learns | "among inspected facilities, who fails?" | "who ends up on the failed register?" |
| acting on it means | choosing whom to inspect | flagging risk regardless of whether anyone would have looked |

Sit with the base-rate line: 27.7% vs 11.6% *on the same data*. Among
facilities the city chose to inspect, more than one in four fail; across all
facilities, one in nine end up on the register. Neither number is wrong — they
answer different questions. Publishing one where the other is expected is how
policy models mislead. The dashboard keeps the distinction visible: each
experiment carries its framing pill, and the %-labeled card explains itself
accordingly.

One more thing the second run demonstrated: the cohort and every feature are
**shared** between the two experiments. triage-pg content-addresses each
artifact over its full input closure, so the EIS run cache-hit the cohort and
feature artifacts the first run built and only rebuilt labels, matrices, and
models. The Derivation tab shows which nodes were reused (marked cache-hit)
— provenance and caching are the same mechanism.

## Features — Deep Feature Synthesis, point-in-time

The `feature_config` describes an entity graph, not feature formulas:
facilities (the target) with inspections as a child event stream, related by
`entity_id`, joined **as-of**:

- facility attributes become fixed-vocabulary one-hots
  (`facilities.facility_type=restaurant`, top-15 types ≈ 96% of entities);
- the inspection history is aggregated over `P1M`/`P3M`/`P6M` windows —
  counts, result/risk/type breakdowns, recency — every aggregate computed
  *as of* each date using only prior events.

featurizer (the DFS engine) expands this into ~30 features and generates the
SQL; you never hand-write an aggregation. Every feature also needs an
imputation rule (here: fit-free zero-fill). The fit-free/fit-based imputation
split is a leakage boundary: anything *fitted* (a mean, a median) is fitted on
the **training split only** and applied to the test split — never computed
over the full matrix.

## The grid, the run, the leaderboard

The committed grid is deliberately small — two decision trees, a random
forest, two scaled logistic regressions (5 groups × 4 splits = 20 models) —
because this tutorial is about the *problem*, not hyperparameters. Read the
results three ways:

```bash
uv run triage --dbfile dirtyduck-database.yaml leaderboard b9e38fd8   # CLI table
uv run triage --dbfile dirtyduck-database.yaml audition b9e38fd8      # selection rules
just serve 8001                                                       # the dashboard
```

Audition is DSSG's model-selection discipline computed in PostgreSQL:
distance-from-best and regret across splits, so you pick a model group for
*stability across time*, not one lucky split. On the model card, the
threshold curve answers the operational question — "if we can inspect the
top k, what precision/recall do we get?" — which is the actual decision an
inspections team makes.

## Fairness and subsets — one identity-neutral block away

Append this to either config (it observes the problem, it doesn't define it —
the experiment hash does not change):

```yaml
bias_config:
  query: |
    select entity_id, facility_type
    from ontology.entities
    where start_time < '{as_of_date}'
  parameter: 100_abs
  intervention: punitive     # an inspection is a burden → FPR/FDR parity matter

evaluation:
  subsets:
    - name: restaurants
      query: |
        select entity_id from ontology.entities
        where facility_type = 'restaurant' and start_time < '{as_of_date}'
```

Re-running with this block cache-hits the entire pipeline and adds the audit:
per-facility-type fairness metrics over the top-100 list (17,200 bias rows on
this data — τ-disparity verdicts in the Bias tab, with the fairness-tree
wizard explaining which metric family your intervention type implies) and a
parallel evaluation restricted to restaurants (120 subset evaluations,
re-ranked *within* the subset). `punitive` matters: when the model's output
burdens people (inspections, audits), you care about who is *wrongly flagged*
— false-positive parity — not who is missed.

## Same data, other targets

DirtyDuck doubles as the problem-type showcase — each variant is a committed
config against the same database, run the same way:

| Config | `problem_type` | Target |
| --- | --- | --- |
| `experiment.yaml` | classification | fails an inspection in 6 months (inspections regime) |
| `experiment-eis.yaml` | classification | appears on the failed register (early-warning regime) |
| `experiment-regression.yaml` | regression_ranking | violation count over the window, ranked |
| `experiment-survival.yaml` | survival | time-to-failure `(duration, event_observed)`, in-PG C-index |
| `experiment-deepgrid.yaml` | classification | a wider grid + a no-categoricals ablation twin |
| `experiment-visits.yaml` | classification | **visit-level** regime: will *this* inspection find a violation? |

## Where this differs from DSSG triage

The discussion above is DSSG's — the two-case framing is the heart of their
Dirty Duck tutorial, and the credit is theirs. What changed underneath:
feature generation moved from collate's aggregate SQL to featurizer's entity
graph; evaluation, audition, and fairness metrics run *inside* PostgreSQL
instead of Python + Aequitas; predictions are append-only; every artifact is
content-addressed (the caching you watched); and the framing distinction DSSG
taught as narrative is a first-class config key with UI. The full
dimension-by-dimension account is the
[honest side-by-side](https://ccd-ia.github.io/triage-pg/triage-pg-vs-dssg-triage.html).

## Where next

- [**Chicago 311**](/triage-pg/tutorials/chicago311/) — an early-warning case
  carried into fairness auditing, monitoring, and survival analysis.
- [`docs/fairness.md`](https://github.com/ccd-ia/triage-pg/blob/main/docs/fairness.md),
  [`docs/problem-types.md`](https://github.com/ccd-ia/triage-pg/blob/main/docs/problem-types.md)
  for the reference treatments.
- `just tutorial-down` when you're done.
