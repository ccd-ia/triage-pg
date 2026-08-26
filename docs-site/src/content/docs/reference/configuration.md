---
title: Configuration reference
description: Every top-level key of an experiment.yaml — purpose, whether it's required, its shape, and the contract the validator enforces.
sidebar:
  order: 5
---

An experiment is one YAML file. `triage run <file>` reads it, and one
function — `validate_experiment_config` in `src/triage/adapters/run.py` —
decides whether it is well-formed. **That validator is the source of truth**;
this page transcribes it. The same validator backs two surfaces you can reach
before committing to a run:

- **`triage analyze-config <file>`** — the CLI dry-run. It runs exactly the
  checks below, touches no database, and prints the derived shape: split count,
  grid size, feature-column count, the feature-group fan-out, the models those
  multiply out to, and the label card. `--estimate` additionally counts the
  cohort and label rows against a live database. See
  [the CLI reference](/triage-pg/reference/cli/).
- **`POST /api/validate-config`** — the write webapp's submission-form check, a
  thin wrapper over the same function (validation is core logic, not
  UI logic).

Both return the same structured result:

```
{valid, experiment_hash, problem_type, n_splits, n_models,
 n_feature_groups, n_runs, errors: [{path, message}], warnings: [str]}
```

Errors are **path-addressed** (`label_config.query`,
`temporal_config.model_update_frequency`, `evaluation.subsets[0].name`) so a
webapp form can point at the offending field. `experiment_hash` is derivable as
soon as the four identity keys are present, even when deeper checks fail.

`n_models` is the grid size **per split, per run**; the total fits are
`n_models × n_splits × n_runs`. `n_feature_groups` and `n_runs` are different
numbers — two groups swept `['all', 'leave-one-out']` is 2 groups but 3 runs,
and it is runs that multiply the cost. Both are `null` when `feature_config`
cannot be planned, meaning *not known* rather than zero.

## Identity vs. attempt (read this first)

An **Experiment** is a *problem*; a **Run** is one *attempt* at it (see [identity and caching](/triage-pg/concepts/identity-and-caching/)). Only
four keys define the problem — its `experiment_hash` is a SHA-256 over their
canonical form:

- `problem_type` · `cohort_config` · `label_config` · `temporal_config`

Everything else — `feature_config`, `grid_config`, `imputation_config`,
`bias_config`, `evaluation`, `sources`, `task_framing`, `name`, `description`,
`config_version` — is either the Run's attempt or identity-neutral metadata.
Changing a feature set, grid, or the framing tag re-attacks the **same**
problem (and may cache-hit); changing the cohort, label, temporal window, or
problem type is a **different** experiment.

## Every top-level key

The validator knows exactly these seventeen keys (`_KNOWN_TOP_LEVEL_KEYS`).
Anything else is dead weight the pipeline silently skips — so the validator
emits a warning to surface a typo or a misplacement instead of letting it pass.

| Key | Required? | In identity? | Purpose |
|---|---|---|---|
| `problem_type` | **required** | yes | What the model predicts and how it's scored. |
| `cohort_config` | **required** | yes | Who is eligible for prediction at each `as_of_date`. |
| `label_config` | **required** | yes | The target `y` — the templated label query. |
| `temporal_config` | **required** | yes | The train/test split windows (fed to timechop). |
| `feature_config` | **required** | no | The featurizer ER-graph (and optional feature groups). |
| `grid_config` | **required** | no | Estimator class paths → hyperparameter grids. |
| `imputation_config` | optional | no | Per-metric imputation rules (defaults to zero-fill). |
| `bias_config` | optional | no | Protected-attribute query + the fairness audit's cut. |
| `evaluation` | optional | no | Metric selection + cohort subsets. |
| `sources` | optional | no | Declared input tables, pinned into the derivation DAG. |
| `task_framing` | optional | no | The observation regime (how to *read* the numbers). |
| `target_history_lags` | optional | yes (matrix) | How many `_target_lag_*` columns to attach for lag baselines. |
| `history_query` | optional | yes (matrix) | The raw periodic series for the raw-series baselines. |
| `history_series_width` | optional | yes (matrix) | Max `_hist_*` width for the raw series. |
| `name` | optional | no | Cosmetic experiment label. |
| `description` | optional | no | Cosmetic free text. |
| `config_version` | optional | no | Recognized but not enforced; reserved. |

The six required keys are checked first; each missing one yields
`{path: "<key>", message: "required key is missing"}`.

---

## `problem_type`

**Purpose.** Selects the score → rank → evaluate machinery: label columns,
estimator family, and evaluation functions. See
[the ranking spine](/triage-pg/reference/problems/) for the full treatment.

**Required.** Part of experiment identity.

**Shape.** One of four string literals:

```yaml
problem_type: classification   # | regression_ranking | regression | survival
```

**Contract.**

- Must be one of `classification`, `regression_ranking`, `regression`,
  `survival`. Anything else:
  `unknown problem_type <x> — expected one of [...]`.
- `survival` additionally requires the survival extra (scikit-survival). If
  `sksurv` is not importable the validator fails with
  `problem_type 'survival' requires the survival extra (scikit-survival) — install with 'uv sync --extra survival'`
.
- It dictates the label columns — see `label_config` below.

## `cohort_config`

**Purpose.** The set of entities eligible for prediction at a given
`as_of_date`; its query becomes the matrix rows.

**Required.** Part of experiment identity.

**Shape.** A mapping with a `query` (and an optional cosmetic `name`):

```yaml
cohort_config:
  name: active_facilities
  query: |
    select e.entity_id
    from ontology.entities as e
    where e.activity_period @> {as_of_date}::date
```

**Contract.**

- Must carry a non-empty `query` string: `cohort_config needs a 'query'`.
- **The cohort query must contain the `{as_of_date}` placeholder** —
  `the cohort query must contain the {as_of_date} placeholder`. The query
  returns one column, `entity_id`.

## `label_config`

**Purpose.** The target `y`. A templated SQL query producing one label row per
cohort entity per as-of date.

**Required.** Part of experiment identity.

**Shape.** A mapping with a `query` (and an optional cosmetic `name`). The
returned **columns follow `problem_type`**:

- `classification` / `regression_ranking` / `regression` → an `outcome` column
  (integer 0/1 for classification; a continuous value for the regression
  family).
- `survival` → a `duration` and an `event_observed` column
  (`event_observed = false` is right-censoring — `duration` is a lower bound,
  not a miss).

```yaml
label_config:
  name: failed_inspections
  query: |
    select entity_id,
           bool_or(result = 'fail')::integer as outcome
    from ontology.events
    where {as_of_date}::date <= date
      and date < {as_of_date}::date + {label_timespan}
    group by entity_id
```

**Contract.**

- Must carry a non-empty `query` string: `label_config needs a 'query'`.
- **The label query must contain both `{as_of_date}` and `{label_timespan}`** —
  each missing placeholder is its own error
  (`the label query must contain the {as_of_date} placeholder`,
  `the label query must contain the {label_timespan} placeholder`).
- Whether you emit `outcome` or `(duration, event_observed)` is enforced
  downstream by the label builder against the declared `problem_type`, not by
  the config validator. Point-in-time correctness — features may use only data
  knowable strictly before the `as_of_date` — is the cardinal rule this query
  and the feature graph must respect (see
  [point-in-time correctness](/triage-pg/concepts/point-in-time-correctness/)).

## `temporal_config`

**Purpose.** The train/test split windows. This is the typed front door to the
inherited timechop engine; the number of splits it produces is
`n_splits` in the validator's result.

**Required.** Part of experiment identity.

**Shape.** Eleven fields, validated by a Pydantic model with `extra="forbid"`
(an unknown or misspelled sub-key fails loudly). Dates are half-open
(`feature_end_time` / `label_end_time` are the day *after* the last included
date). Intervals are Postgres-interval strings, normalized so `'6month'` and
`'6 months'` are identical — note `m` means **minutes**, so months must be
spelled out.

```yaml
temporal_config:
  feature_start_time: '2014-01-01'
  feature_end_time: '2017-07-01'
  label_start_time: '2015-01-01'
  label_end_time: '2017-07-01'
  model_update_frequency: '6month'
  training_as_of_date_frequencies: '6month'
  training_label_timespans: ['6month']
  test_as_of_date_frequencies: '6month'
  test_durations: '0day'
  test_label_timespans: ['6month']
  max_training_histories: '5year'
```

**Contract.**

- All four dates plus `model_update_frequency` are required; the six
  frequency/history/duration/timespan fields each accept a single interval or a
  non-empty list of intervals (an empty list is rejected).
- A convenience key `label_timespans` is accepted and expands to both
  `training_label_timespans` and `test_label_timespans` unless an explicit
  per-side value is already present.
- `feature_start_time` must not be after `feature_end_time` (same for the label
  window). Pydantic validation errors are surfaced path-addressed under
  `temporal_config.<field>`.
- If the resulting windows yield no splits the run fails with
  `Timechop produced no train/test splits for this temporal_config — widen the
  feature/label windows or shorten the label_timespan`.
- Note the example's `test_durations: '0day'`: a deliberate point-in-time teaching
  default that yields exactly **one** evaluation point per model per metric.
  `test_durations` × `test_as_of_date_frequencies` decides how many prediction
  times each model is evaluated at — the evaluation grain, what it buys you, and
  how to pick a cadence are covered in [Evaluation](/triage-pg/reference/evaluation/).

## `feature_config`

**Purpose.** The featurizer ER-graph — entities, variables, relationships, and
aggregation intervals — that Deep Feature Synthesis turns into feature columns.
triage concepts never leak into featurizer.

**Required.** **Not** in identity — it belongs to the Run's attempt.

**Shape.** A non-empty mapping. `feature_groups` nests **under**
`feature_config`:

```yaml
feature_config:
  target: facilities
  max_depth: 2
  intervals: [P1M, P3M, P6M]     # ISO-8601 (featurizer format)
  entities:
    - alias: facilities
      id: entity_id
      table: ontology.entities
      variables: { facility_type: { type: categorical, role: categorical } }
    - alias: inspections
      id: ~
      table: ontology.events
      temporal_ix: date          # the knowledge date on child event streams
      variables: { result: { type: categorical } }
  relationships:
    - parent: { entity: facilities, key: entity_id }
      child:  { entity: inspections, key: entity_id }
      temporal: { mode: as_of }
  # optional — expands ONE experiment into several runs:
  feature_groups:
    group_by: source_entity
    strategies: [all, leave-one-out, leave-one-in, all-combinations]
    all_combinations_max_groups: 6
```

**Contract.**

- Must be a non-empty mapping:
  `feature_config must be a non-empty mapping (the featurizer ER-graph config)`.
- `feature_groups` belongs here, not at the top level (see the warning below).
  Both `definitions` and `group_by` partitions are resolved at validate time —
  the feature manifest is derived from `feature_config` with no database — so
  `n_feature_groups` and `n_runs` are known before anything runs.
- **Write `definitions` globs against the feature's label.** A generated feature
  name longer than PostgreSQL's 63-byte identifier limit is hash-truncated from
  the tail (`AVG(consultas.frecuencia_cardiaca_en_reposo|interval=P~67a3dcf5`),
  so a glob aimed at a fragment past the cut would never match the physical
  column — and the hash is not something you can write a glob for. Globs are
  therefore matched against each column's full, untruncated **label** as well as
  its physical name, so `["*frecuencia_cardiaca*"]` does what you mean. To see
  what a glob resolves to before running anything:

  ```console
  $ triage analyze-config experiment.yaml --features '*(inspections.*'

  120 of 147 match '*(inspections.*'  (0 truncated)
    ABS(facilities.COUNT(inspections.date))
    ABS(facilities.COUNT(inspections.date|interval=P1M))
    ...
  ```

  `--features '*'` lists every feature column. Truncated columns are shown as
  `label → column`. The command needs no database.
- Every column must land in **exactly one** group. A column matching no glob, or
  more than one, is a loud error rather than a silent drop or double-count.
- **A fan-out and a name-pinned baseline can contradict each other.** If
  `grid_config` holds a baseline that selects its feature by name
  (`BaselineRankMultiFeature`, `SimpleThresholder`, `PercentileRankOneFeature`,
  `LinearRanker`) and a strategy produces a run without that column, `triage run`
  refuses the config before building anything, rather than training the runs that
  work and dying on the one that cannot. `triage analyze-config` reports the same
  conflict without running.

## `grid_config`

**Purpose.** The estimator search space. Each estimator's hyperparameter lists
are Cartesian-producted into concrete models; the total across all estimators
is `n_models` (per split). Multiply by splits **and** by feature-group runs for
the real fit count — `triage analyze-config` prints it as *Models to be trained*.

**Required.** **Not** in identity — the Run's attempt.

**Shape.** A mapping of fully-qualified estimator `class_path` →
`{hyperparameter: [values]}`:

```yaml
grid_config:
  'sklearn.ensemble.RandomForestClassifier':
    n_estimators: [10]
    max_depth: [3]
  'triage.component.catwalk.estimators.classifiers.ScaledLogisticRegression':
    C: [0.01, 1.0]
    penalty: ['l2']
```

**Contract.**

- Must be a mapping:
  `grid_config must be a mapping {class_path: {hyperparam: [values]}}`.
- An estimator with no hyperparameters yields a single default model; an
  entirely empty grid fails with
  `grid is empty — at least one estimator class_path is required`.
- Estimators are resolved by class path — any sklearn estimator, triage's
  `ScaledLogisticRegression` (min-max scaling + LR, so coefficients are
  comparable and persisted as signed β / odds ratios), or, for `survival`, the
  scikit-survival wrappers such as
  `triage.component.catwalk.estimators.survival.ScaledCoxPHSurvivalAnalysis`.

## `imputation_config`

**Purpose.** Per-metric imputation rules. Every feature needs an explicit rule;
the fit-free / fit-based split is a leakage boundary.

**Optional.** Defaults to `{"all": {"type": "zero"}}`. Not in identity (it does
enter the matrix's derivation hash).

**Shape.** A mapping of metric name (`count`, `sum`, `max`, …) → rule; the
reserved key `all` is the fallback for any metric without an explicit rule:

```yaml
imputation_config:
  count:
    type: zero_noflag
  all:
    type: zero
```

**Contract.**

- Each rule's `type` is one of `zero`, `zero_noflag`, `constant`,
  `null_category`, `mean`, `median`, `mode`, `binary_mode`, `error`. The policy
  must define at least one rule.
- `type: constant` **requires** a `value`; any other type must **not** carry a
  `value`.
- **Fit-free** rules (zero/constant/null_category + the `*_imp` flag) compute
  nothing from the data and are safe anywhere. **Fit-based** rules
  (`mean`/`median`/`mode`/`binary_mode`) compute a statistic that is fitted on
  the *training split only* and applied to both train and test — never fit a
  statistic over the full matrix.

## `bias_config`

**Purpose.** Drives the in-Postgres fairness audit — ingests protected
attributes and pins the top-k cut it audits at. Identity-neutral: it
observes the problem, it does not define it.

**Optional.**

**Shape.**

```yaml
bias_config:
  query: |
    select entity_id, race, sex
    from ontology.demographics
    where knowledge_date < '{as_of_date}'
  parameter: 100_abs           # required — the top-k cut
  ref_groups: { race: White }  # optional reference pins
  tau: 0.8                     # optional four-fifths threshold
  intervention: punitive       # optional
```

**Contract.**

- Must be a mapping with a `query`. **The query needs the `{as_of_date}`
  placeholder and returns `entity_id` plus one column per protected
  attribute** (melted to long form in `triage.protected_groups`).
- `parameter` is required — the top-k cut the audit runs at, e.g. `100_abs`
  or `10_pct`.
- `tau` (default `0.8`, the four-fifths rule) must be a number in `(0, 1]`.
- `intervention`, when present, is one of `punitive`, `assistive`,
  `representation` (it routes the fairness tree's attention; it never hides
  metrics). `ref_groups`, when present, is a mapping of
  `{attribute: reference_value}`.

## `evaluation`

**Purpose.** Overrides the problem-type default metric set and declares cohort
**subsets**. Identity-neutral.

**Optional.** Defaults by problem type: classification →
`metrics: [precision@, recall@, auc_roc, average_precision]`,
`thresholds: [100_abs, 10_pct]`; the regression family →
`[rmse, mae, r2, pinball@0.5, pinball@0.8, pinball@0.95]` (pinball@τ is the
quantile-loss metric a τ-quantile forecaster minimizes, migration 0020);
survival → `[c_index]`.

**Shape.** The `triage.evaluate_model` jsonb shape
(`metrics` / `thresholds` / `regression_metrics` / `survival_metrics`), plus
`subsets`:

```yaml
evaluation:
  regression_metrics: [rmse, mae]     # override the default (which adds r2)
  subsets:
    - name: high_risk_zips
      query: |
        select entity_id
        from ontology.entities
        where zip_code = any('{60622,60647}') and {as_of_date} is not null
```

**Contract.**

- The metric keys override the problem-type default when present; a
  subsets-only block still falls back to the default metric set.
- `subsets`, when present, must be a list of `{name, query}` mappings. Each
  needs a non-empty, **unique** `name` (`duplicate subset name <x>` otherwise)
  and a `query` returning `entity_id`. **Each subset query must contain the
  `{as_of_date}` placeholder.** A subset is re-ranked within itself — its
  precision@k is the top-k of the subset's own ranking.

## `target_history_lags`, `history_query`, `history_series_width`

**Purpose.** Expose each entity's own prior target values, point-in-time-correctly,
for the [time-series baselines](/triage-pg/reference/baselines/). See
[target history](/triage-pg/concepts/target-history/) for the leakage boundary.
All three enter **matrix identity** (a change rebuilds the matrix).

**Optional.** Resolved automatically from `grid_config`: a **lag-family** baseline
(`Persistence`, `PromedioDisponible`, `MovingAverage`, `Drift`) turns on the lags;
a **raw-series** baseline (`SeasonalNaive`, `ETS`, `HoltWinters`, `Croston`,
`CrostonSBA`) **requires** a `history_query` (a hard error at run start otherwise).

**Shape.**

- `target_history_lags` (int, default 12 when a lag baseline is present) — how many
  reserved `_target_lag_*` columns (windowed-label lags, admissible where
  `t + w ≤ as_of_date`) to attach.
- `history_query` (SQL, no default) — a period-level aggregation returning
  `entity_id, period, value`, with an `{as_of_date}` placeholder and its own
  knowledge-date discipline (`where knowledge_date < {as_of_date}`). Pivoted into
  reserved `_hist_*` columns.
- `history_series_width` (int, default 24) — the max `_hist_*` width.

```yaml
problem_type: regression_ranking

target_history_lags: 6
history_series_width: 24
history_query: |
  select entity_id, date_trunc('month', date)::date as period, count(*) as value
  from ontology.events
  where date < {as_of_date}          -- point-in-time correct
  group by 1, 2

grid_config:
  'sklearn.ensemble.RandomForestRegressor': { n_estimators: [100] }
  # lag floors (read _target_lag_*; no history_query needed):
  'triage.component.catwalk.baselines.timeseries.Persistence': {}
  'triage.component.catwalk.baselines.timeseries.MovingAverage': { window: [3, 6] }
  # raw-series floors (need history_query above; HoltWinters needs triage[baselines]):
  'triage.component.catwalk.baselines.timeseries.SeasonalNaive': { season: [12] }
  'triage.component.catwalk.baselines.timeseries.HoltWinters': {}
  'triage.component.catwalk.baselines.timeseries.Croston': {}

evaluation:
  regression_metrics: [rmse, mae, r2, pinball@0.5, pinball@0.9]
```

**Contract.** The reserved `_target_lag_*` / `_hist_*` columns are excluded from
the feature set and imputation; a baseline that has no history for an entity
(cold start) *abstains* — it emits no prediction for that row, so the metric is
computed over the entities it could score.

## `sources`

**Purpose.** The declared input tables cohort/label/feature queries read. Only
declared sources enter artifact identity (there is no SQL parsing), and pinning
each is what makes downstream derivations cacheable (ADRs 0013–0017).

**Optional** — but strongly recommended.

**Shape.** A list of source mappings:

```yaml
sources:
  - name: ontology_events
    relation: ontology.events
    knowledge_date_column: date
    version_label: 'dirtyduck-v1'   # static → idempotent re-runs
    role: event
    type_column: type
    description: Food inspection events (DirtyDuck tutorial)
```

**Contract.**

- The validator does not reject a missing `sources` block, but it **warns**:
  `no sources declared — every derivation is volatile (never a cache hit) and
  inputs are unpinned`. A source without a `version_label` is
  volatile and forces a rebuild every run.

## `task_framing`

**Purpose.** The observation regime — who gets a label and why. It changes how
you *read* the numbers, not how they're computed; the dashboard turns it into a
pill and adjusts the %-labeled expectation. See
[the full problem space](/triage-pg/reference/problems/).

**Optional.** Identity-neutral by construction (migration 0019) — adding or
changing it never forks an experiment's hash.

**Shape.**

```yaml
task_framing: resource_prioritization   # | early_warning | visit_level
```

**Contract.**

- One of `early_warning`, `resource_prioritization`, `visit_level`. Anything
  else: `unknown task_framing <x> — expected one of [...]`.

## `name`, `description`

**Purpose.** Cosmetic experiment metadata stored on the experiment row (with
the OS user as `author`). Kept out of identity — a re-run keeps the first
writer's values.

**Optional.** Neither is validated beyond being recognized.

```yaml
name: DirtyDuck failed-inspections baseline
description: Which facilities fail an inspection in the next 6 months?
```

## `config_version`

**Purpose.** A reserved slot for pinning the config schema version.

**Optional.** Recognized (so it never triggers the unknown-key warning), but the
validator does **not** currently enforce a value or read it during a run.

---

## Placeholder contracts, in one place

The templated queries are substituted at build time; the validator checks the
required placeholders are literally present:

| Block | Required placeholders | Returns |
|---|---|---|
| `cohort_config.query` | `{as_of_date}` | `entity_id` |
| `label_config.query` | `{as_of_date}`, `{label_timespan}` | `outcome` (or `duration, event_observed`) |
| `evaluation.subsets[].query` | `{as_of_date}` | `entity_id` |
| `bias_config.query` | `{as_of_date}` | `entity_id` + one column per protected attribute |

## Warnings the validator emits

Warnings never make a config invalid — they surface silent misbehavior:

- A misplaced `feature_groups` at the top level:
  `top-level 'feature_groups' is ignored — nest it under
  feature_config.feature_groups to get the fan-out`.
- Any other unrecognized top-level key:
  `unknown top-level key '<x>' is ignored` (this is how a typo like
  `label_confg` surfaces instead of being silently skipped).
- No `sources` declared (the volatility warning above).

## Worked examples

The committed configs exercise every key against the tutorial databases:
`example/dirtyduck/experiment.yaml` (classification, resource-prioritization
framing), `experiment-eis.yaml` (early-warning twin),
`experiment-regression.yaml` (`regression_ranking` with an `evaluation`
override), `experiment-survival.yaml` (survival label columns + scikit-survival
grid), and `experiment-visits.yaml` (visit-level framing).

## Where next

- [The problem space](/triage-pg/reference/problems/) — the two axes
  (`problem_type` and `task_framing`) in full.
- [The CLI reference](/triage-pg/reference/cli/) — `triage analyze-config` and
  the rest of the surface.
- [Point-in-time correctness](/triage-pg/concepts/point-in-time-correctness/)
  and [identity and caching](/triage-pg/concepts/identity-and-caching/) — the
  two rules the query and identity keys serve.
</content>
</invoke>
