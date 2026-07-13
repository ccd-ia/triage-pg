---
title: The problem space — two orthogonal axes
description: The four problem types (what the model predicts) and the three observation regimes (who gets a label and why) — exhaustively, one axis at a time.
sidebar:
  order: 0
  label: The problem space
---

Every triage-pg experiment is located on **two orthogonal axes**, declared by
two config keys:

- **`problem_type`** — *what the model predicts and how it's scored*:
  `classification` · `regression_ranking` · `regression` · `survival`.
  This axis is part of the experiment's identity (changing it is a new
  problem) and drives the machinery: label columns, estimator family,
  evaluation functions.
- **`task_framing`** — *the observation regime*: who gets a label and why —
  `early_warning` · `resource_prioritization` · `visit_level`. This axis is
  identity-neutral metadata: it changes how you should *read* the numbers,
  not how they're computed.

The axes compose freely; this page deliberately teaches each axis **once**
rather than enumerating the combinations — at the end you'll see why no
matrix is needed.

Shared background for everything below: whatever the problem type, triage-pg
runs the same **score → rank → evaluate** spine (ADR-0010). The model emits a
per-entity score; entities are ranked by it; evaluation reads the ranked,
append-only predictions in the database. The problem types differ in what the
score *means* and which evaluation functions apply.

---

## Axis 1 — the four problem types

### `classification`

**Posing the question.** "Will X happen to this entity within the label
window?" — *will this facility fail an inspection in 6 months? will this
request take more than 14 days?* The policy team's yes/no question, asked at
a moment in time.

**Label shape.** One row per entity with a binary `outcome` (0/1). From the
DirtyDuck config:

```sql
select entity_id,
       bool_or(result = 'fail')::integer as outcome
from ontology.events
where {as_of_date}::date <= date
  and date < {as_of_date}::date + {label_timespan}
group by entity_id
```

**What the model outputs.** A score in [0, 1] — the estimator's positive-class
probability — used as the ranking key. The score is *not* a decision; the
top-k cut you act on is chosen at evaluation/deployment time, not baked into
training.

**Evaluation.** Defaults: `precision@` and `recall@` at the `100_abs` and
`10_pct` cuts, `auc_roc`, `average_precision` — all PL/pgSQL over the
predictions table. Precision@k is the operational metric ("if we act on the
top k, how often are we right?"); AUC summarizes ranking quality
independently of any cut.

**Estimators.** Any sklearn classifier by class path
(`sklearn.tree.DecisionTreeClassifier`,
`sklearn.ensemble.RandomForestClassifier`, …) plus triage's
`ScaledLogisticRegression` (min-max scaling + LR, so coefficients are
comparable and persisted as signed β / odds ratios).

**Characteristics — when to choose it.** The default for policy triage:
outcomes are naturally binary (fail/no-fail, slow/fast, funded/unfunded), the
deliverable is a ranked list with a capacity cut, and stakeholders reason in
precision/recall terms.

**Pitfalls.** Class imbalance makes accuracy meaningless — always read
metrics against the base rate. Don't threshold the score at 0.5 "because
probability": the cut is a *capacity* decision. And a binary label throws
away magnitude — if "how much/how long" matters, look at the other three
types.

**Worked example.**

```yaml
problem_type: classification
label_config:
  name: failed_inspections
  query: |
    select entity_id, bool_or(result = 'fail')::integer as outcome
    from ontology.events
    where {as_of_date}::date <= date
      and date < {as_of_date}::date + {label_timespan}
    group by entity_id
```

Full committed config:
[`example/dirtyduck/experiment.yaml`](https://github.com/ccd-ia/triage-pg/blob/main/example/dirtyduck/experiment.yaml)
— run end-to-end in the [DirtyDuck tutorial](/triage-pg/tutorials/dirtyduck/).

### `regression_ranking`

**Posing the question.** "How *much* of X will this entity accumulate — and
who accumulates the most?" — *how many violations will this facility rack up?*
The target is continuous, but the deliverable is still a ranked list: you
care about *who is worst*, more than about the exact number.

**Label shape.** One row per entity with a continuous `outcome`. From the
committed regression config:

```sql
select entity_id,
       sum(coalesce(jsonb_array_length(violations), 0))::double precision as outcome
from ontology.events
where {as_of_date}::date <= date
  and date < {as_of_date}::date + {label_timespan}
group by entity_id
```

**What the model outputs.** The predicted magnitude, used directly as the
ranking key — the top of the list is "predicted most violations".

**Evaluation.** The regression family defaults: `rmse`, `mae`, `r2` —
config-selectable via the `evaluation:` block (the committed example selects
`rmse` + `mae` only, to show the override). The rank columns still populate,
so top-k lists and the dashboard's ranked views work exactly as in
classification.

**Estimators.** sklearn regressors by class path
(`sklearn.ensemble.RandomForestRegressor`, linear models, …).

**Characteristics — when to choose it.** ADR-0010 makes this the **primary
path for continuous targets**: it keeps the ranked-list deliverable policy
teams act on, while training on the richer continuous signal instead of a
binarized version of it.

**Pitfalls.** RMSE is dominated by the tail on skewed count targets — read it
next to MAE. Don't collapse the continuous label to 0/1 "to make it
classification"; if you find yourself choosing a threshold to binarize,
you're usually better off here. Rank ties at zero (many entities with no
events) are real — the interesting ranking lives in the tail.

**Worked example.**

```yaml
problem_type: regression_ranking
label_config:
  name: violation_count
  query: |
    select entity_id,
           sum(coalesce(jsonb_array_length(violations), 0))::double precision as outcome
    from ontology.events
    where {as_of_date}::date <= date
      and date < {as_of_date}::date + {label_timespan}
    group by entity_id
evaluation:
  regression_metrics: [rmse, mae]   # override; default adds r2
```

Full committed config:
[`example/dirtyduck/experiment-regression.yaml`](https://github.com/ccd-ia/triage-pg/blob/main/example/dirtyduck/experiment-regression.yaml).

### `regression`

**Posing the question.** "What will X's value *be*?" — pure point prediction,
where the number itself is the deliverable: a forecasted cost, a caseload, a
duration used downstream in arithmetic.

**Label shape.** Identical to `regression_ranking` — a continuous `outcome`
(the committed pure-regression config reuses the violation-count label
verbatim). The two types differ in *intent and evaluation emphasis*, not
label schema: declaring `regression` says "the magnitude is the product",
declaring `regression_ranking` says "the ordering is the product".

**What the model outputs.** The predicted value. Ranking columns are still
computed (the spine is shared), but nothing downstream assumes a capacity
cut.

**Evaluation.** `rmse`, `mae`, `r2` (the family default; config-selectable —
the committed example requests all three explicitly).

**Estimators.** The same sklearn regressor family as `regression_ranking`.

**Characteristics — when to choose it.** When a consumer needs the number:
budgeting, staffing arithmetic, feeding another model. If any human will read
the output as "who first?", prefer `regression_ranking` — same label, more
honest framing.

**Pitfalls.** R² on temporal splits can mislead (the target's variance
shifts across time — compare within a split, not across). A good RMSE can
coexist with a useless ranking and vice versa; declare the type by what you
will actually consume.

**Worked example.**

```yaml
problem_type: regression
label_config:
  name: violation_count
  query: |            # same continuous label as regression_ranking
    select entity_id,
           sum(coalesce(jsonb_array_length(violations), 0))::double precision as outcome
    from ontology.events
    where {as_of_date}::date <= date
      and date < {as_of_date}::date + {label_timespan}
    group by entity_id
evaluation:
  regression_metrics: [rmse, mae, r2]
```

Full committed config:
[`example/dirtyduck/experiment-pure-regression.yaml`](https://github.com/ccd-ia/triage-pg/blob/main/example/dirtyduck/experiment-pure-regression.yaml).

### `survival`

**Posing the question.** "How *long* until X happens — knowing that for some
entities we'll stop watching before it does?" — *time to resolution, time to
failure, time to re-entry.* The question classification throws away twice:
magnitude *and* the difference between "didn't happen" and "hadn't happened
yet when we stopped looking".

**Label shape.** Two columns per entity — `(duration, event_observed)`
(ADR-0010's survival-ready label schema). `event_observed = false` is
**censoring**: the window closed first, so `duration` is a *lower bound*, not
a miss. From the Chicago 311 survival config:

```sql
select
  e.entity_id,
  case
    when e.closed_date is not null
     and e.closed_date < {as_of_date}::date + {label_timespan}
    then extract(epoch from (e.closed_date - e.created_date)) / 86400.0
    else extract(epoch from (({as_of_date}::date + {label_timespan}) - e.created_date)) / 86400.0
  end as duration,
  (e.closed_date is not null
   and e.closed_date < {as_of_date}::date + {label_timespan}) as event_observed
from ontology.entities as e
where e.created_date >= {as_of_date}::date - interval '1 month'
  and e.created_date <  {as_of_date}::date
```

**What the model outputs.** A risk score — higher means the event is expected
*sooner*. It ranks like any other score; the semantics are relative hazard,
not a probability or a duration.

**Evaluation.** The **concordance index** (`c_index`) — of all comparable
pairs, how often does the higher-risk entity experience the event first? —
computed by a PL/pgSQL function that matches scikit-survival's
`concordance_index_censored` to 1e-9 (ADR-0026). Censored rows participate
exactly as far as they're comparable, which is the entire point.

**Estimators.** scikit-survival behind the `survival` extra
(`uv sync --extra survival`) — the committed wrapper is
`ScaledCoxPHSurvivalAnalysis` (scaled Cox proportional hazards).

**Characteristics — when to choose it.** Whenever "how long" is the real
question and censoring is real — open tickets, ongoing cases, subscriptions.
The dashboard adapts: the entity drawer shows `duration` + event/censored per
label row, and the header's base-rate card becomes an **event rate** (share
of labels whose event was observed).

**Pitfalls.** Treating censored rows as "no event = 0" silently converts the
problem to biased classification — the single most common survival mistake.
Duration units are whatever your SQL emits (days here) — be consistent. A
C-index needs comparable pairs: a window so short that almost nothing is
observed leaves it undefined.

**Worked example.**

```yaml
problem_type: survival        # requires: uv sync --extra survival
label_config:
  name: time_to_resolution
  query: |                    # emits (duration, event_observed) — see above
    …
grid_config:
  'triage.component.catwalk.estimators.survival.ScaledCoxPHSurvivalAnalysis':
    alpha: [0.1]
```

Full committed configs:
[`example/chicago311/experiment-survival.yaml`](https://github.com/ccd-ia/triage-pg/blob/main/example/chicago311/experiment-survival.yaml)
and
[`example/dirtyduck/experiment-survival.yaml`](https://github.com/ccd-ia/triage-pg/blob/main/example/dirtyduck/experiment-survival.yaml)
— run live in the [Chicago 311 tutorial](/triage-pg/tutorials/chicago311/).

---

## Axis 2 — the three observation regimes

`problem_type` says what the model predicts. **`task_framing`** says
something the math can't: *under what conditions does reality hand you a
label?* It's an optional, identity-neutral config key (adding or changing it
never forks an experiment's hash) that the dashboard turns into a pill beside
the problem type and into context on the %-labeled card.

### `early_warning`

**Observation semantics.** The outcome is **administratively recorded for
every cohort member** — a register, a ledger, a system of record closes every
case. Nobody has to act for the truth to exist.

**%labeled expectation.** ~**100%** once the window matures. The dashboard
treats less than that as a warning sign ("labels should cover the cohort") —
in this regime, missing labels mean a broken label query or broken data, not
a fact of life.

**What the base rate means.** The *population* prevalence: "X% of all
requests are slow", "Y% of all projects go unfunded". It's the number you may
quote publicly.

**Selection-bias implications.** Minimal on the label side — the model learns
from everyone. (Your cohort definition can still select; the label doesn't.)

**How you act on the list.** Flag, escalate, prioritize attention — the
entity would have its outcome regardless; you're choosing where to *look
early*, and even ignoring the list costs you nothing in future label
coverage.

**Config + example.**

```yaml
task_framing: early_warning
# the signature move: absence of the event is a real 0, knowable for everyone
label_config:
  query: |
    select e.entity_id,
           coalesce(bool_or(ev.result = 'fail'), false)::integer as outcome
    from ontology.entities e
    left join ontology.events ev on …   -- LEFT JOIN + coalesce = full coverage
    where …cohort condition…
    group by e.entity_id
```

Committed examples: Chicago 311's `slow_resolution` (resolution is recorded
for every request) and DirtyDuck's EIS twin
([`experiment-eis.yaml`](https://github.com/ccd-ia/triage-pg/blob/main/example/dirtyduck/experiment-eis.yaml)).

### `resource_prioritization`

**Observation semantics.** The outcome exists **only for entities someone
acted on** — inspected, audited, visited. For the rest, the truth was never
generated: their label is NULL, meaning *unknown*, not "no".

**%labeled expectation.** **Well under 100%** — the action rate. DirtyDuck's
base config sits at ~54%. The dashboard shows "selective labels — <100%
expected" instead of alarming.

**What the base rate means.** A **conditional** rate: "among *inspected*
facilities, 28% fail". It is not the population rate and must never be quoted
as one — DirtyDuck's twin configs put the same data at 0.277 conditional vs
0.116 population.

**Selection-bias implications.** The big one. The model trains on
entities *selected by the historical process* (complaints, schedules, human
judgment), so it learns "among the kind of places that get inspected…".
Deploying it changes who gets inspected, which changes future labels — the
feedback loop is intrinsic to the regime, and pretending the model speaks
about the whole population is the classic failure.

**How you act on the list.** The list *is* the action: it decides who gets
the scarce resource. Fairness auditing matters most here (the intervention is
often a burden — the fairness tree's "punitive" branch), and next period's
labels will come from whomever you chose.

**Config + example.**

```yaml
task_framing: resource_prioritization
# the signature move: labels come FROM the action stream; no row = unknown
label_config:
  query: |
    select entity_id, bool_or(result = 'fail')::integer as outcome
    from ontology.events          -- only acted-on entities appear here
    where …window…
    group by entity_id
```

Committed example: DirtyDuck's base
[`experiment.yaml`](https://github.com/ccd-ia/triage-pg/blob/main/example/dirtyduck/experiment.yaml)
— the [DirtyDuck tutorial](/triage-pg/tutorials/dirtyduck/) builds its
central lesson on the contrast with the EIS twin.

### `visit_level`

**Observation semantics.** The label attaches to an **event, not to an
entity-period**: each visit/interaction/transaction gets its own outcome
("did *this* visit end in a violation? did *this* call resolve the issue?").
An entity can contribute many labeled rows per window — or none, if it had no
events.

**%labeled expectation.** 100% *of events* — every visit that happened has an
outcome — but coverage is event-driven: the cohort row count tracks activity,
not the entity universe.

**What the base rate means.** A **per-event** rate: "X% of visits end in a
violation." Quoting it as an entity-level risk conflates busy entities with
risky ones.

**Selection-bias implications.** Inherited from whatever generates the
events: if visits are scheduled by risk, the event stream itself is selected.
The regime is honest about the *unit* (the visit) but not automatically about
*which* visits exist.

**How you act on the list.** Per-event routing and preparation: which
upcoming visits need the senior inspector, which incoming calls get the
specialist queue — decisions about *occasions*, not standing entity
designations.

**Config + example.** The cohort's `entity_id` is the *event* (the visit),
not the long-lived actor behind it — from the committed DirtyDuck variant
("will *this* inspection find a violation?"):

```yaml
task_framing: visit_level
# the signature move: the cohort row IS the event
cohort_config:
  name: upcoming_visits
  query: |
    select ev.event_id as entity_id
    from ontology.events as ev
    where {as_of_date}::date <= ev.date
      and ev.date < {as_of_date}::date + interval '1 month'
label_config:
  name: visit_finds_violation
  query: |
    select ev.event_id as entity_id,
           (ev.result = 'fail')::integer as outcome
    from ontology.events as ev
    where {as_of_date}::date <= ev.date
      and ev.date < {as_of_date}::date + {label_timespan}
```

Full committed config:
[`example/dirtyduck/experiment-visits.yaml`](https://github.com/ccd-ia/triage-pg/blob/main/example/dirtyduck/experiment-visits.yaml)
— note its two honesty caveats, stated in the file: the historical visits
stand in for a schedule table (the standard visit-level approximation), and
the visit's `type` is deliberately *not* a feature (a complaint-triggered
visit's existence is only knowable when the complaint arrives).

---

## Why there is no 4×3 matrix

The axes answer different questions — *what does the score mean?* versus
*under what conditions does reality label the data?* — and they compose
without interaction terms: a survival label can be fully observed
(early-warning) or generated only by inspections (resource-prioritization);
a classification label can attach to visits. Teaching twelve combinations
would repeat the same two lessons twelve times. The living proof that the
axes are independent is DirtyDuck itself: **three committed configs on one
dataset, one per regime, all `classification`** — the base (inspections),
the EIS twin (early warning), and the visits variant (visit-level) differ
only in cohort/label SQL and the framing tag, while the model machinery
never notices.

Where to see each axis exercised for real: the
[tutorials](/triage-pg/tutorials/) (all four problem types, both entity-level
regimes) and the terse in-repo reference
[`docs/problem-types.md`](https://github.com/ccd-ia/triage-pg/blob/main/docs/problem-types.md).
