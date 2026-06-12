# triage-pg — Results Schema Redesign (Proposal)

- Status: **Proposal** — all six §8 open questions resolved 2026-06-05; sources registry added 2026-06-11 (§4.7, §8.7); reflected in the alembic baseline
- Date: 2026-06-04
- Implements: ADR-0002 (db-per-project + registry), 0003 (plain-PG), 0005 (Parquet/S3 pointers), 0006 (append-only predictions), 0007 (in-PG eval + SQL bias), 0010 (problem_type + survival-ready labels), 0011 (feature importances persisted), 0014 (source-data pinning)

This proposes the greenfield schema that replaces old triage's results schema. It is a **clean break** (ADR-0001) — no migration path. Read §1 for what's wrong with the old schema, §3–4 for the new DDL, §8 for the decisions I need from you.

---

## 1. Old schema — analysis

Old triage uses **four** PostgreSQL schemas in one flat (single-tenant) database:

| schema | tables |
|---|---|
| `triage_metadata` | experiments, retrain, triage_runs, subsets, model_groups, matrices, models, experiment_matrices, experiment_models, retrain_models |
| `train_results` | predictions, prediction_metadata, evaluations, feature_importances, aequitas |
| `test_results` | predictions, prediction_metadata, evaluations, individual_importances, aequitas |
| `triage_production` | predictions, prediction_metadata |

**Problems to fix:**

1. **3× duplicated `predictions`** (train/test/production) and **2× duplicated `evaluations`/`aequitas`** — identical structures copy-pasted across schemas. → one table + a `split_kind` discriminator.
2. **`predictions` overwrite on re-run** — PK `(model_id, entity_id, as_of_date)`, no `scored_at`. Forecloses monitoring (ADR-0006).
3. **Binary-only labels** — `label_value Integer`, `evaluations.num_positive_labels`. No regression/survival (ADR-0010).
4. **`aequitas` is a 50+-column wide dump** of one library's output — un-queryable, library-coupled (ADR-0007).
5. **Free-string `metric`/`parameter`** in evaluations; no `problem_type` anywhere.
6. **Inconsistent types** — `JSON` vs `JSONB`; naive `DateTime` mostly, `timezone=True` in two places; `String` hashes; `Numeric(6,5)` score (probability-only, breaks for regression).
7. **No tenancy** — single flat namespace (ADR-0002).
8. **Weak referential integrity** — `Model.delete()` hand-rolls a cascade in Python because there are no `ON DELETE CASCADE` FKs.
9. **Denormalized counts** on `experiments` (`time_splits`, `as_of_times`, …) and execution-env detail on `triage_runs` (`ec2_instance_type`, `os_user`, `working_directory`, `installed_libraries`) — derivable or belongs in logs, not the results DB.
10. **`model_type`/`hyperparameters` duplicated** on both `models` and `model_groups`; `model_group` identity computed via a **stored procedure** instead of a deterministic hash.
11. **4 rank columns** (`rank_abs_no_ties`, `rank_abs_with_ties`, `rank_pct_no_ties`, `rank_pct_with_ties`) stored — derivable in SQL at read time.

---

## 2. New architecture — two databases (ADR-0002)

- **Registry DB** — one per instance, the control plane: projects, users, membership, routing, audit. Holds **no DB credentials** (cloud uses IAM, local uses env — ADR-0004).
- **Per-project DB** — one per project; *is* the tenant boundary, so **no `project_id` columns** anywhere inside it. Holds the results schema below in a single `triage` schema.

Modern/safe baseline applied everywhere: `timestamptz` (never naive), `jsonb` (never `JSON`), `gen_random_uuid()` surrogate keys where there's no natural hash, `generated always as identity` for serials, native `enum`s instead of free strings, real FKs with `on delete cascade`, `check` constraints, `not null` by default, deterministic content-hashes as natural keys.

---

## 3. Registry DB

```sql
create schema if not exists registry;

create table registry.projects (
    project_id     uuid primary key default gen_random_uuid(),
    slug           text not null unique,                       -- url-safe; also the per-project DB name
    display_name   text not null,
    database_name  text not null unique,                       -- target DB in the shared cluster
    status         text not null default 'active'
                     check (status in ('active', 'archived')),
    created_at     timestamptz not null default now(),
    archived_at    timestamptz
    -- NO credentials: cloud → IAM role per project; local → env. (ADR-0004)
);

create table registry.users (
    user_id      uuid primary key default gen_random_uuid(),
    email        text not null unique,
    display_name text,
    is_admin     boolean not null default false,
    created_at   timestamptz not null default now()
);

create table registry.project_members (
    project_id uuid not null references registry.projects(project_id) on delete cascade,
    user_id    uuid not null references registry.users(user_id)       on delete cascade,
    role       text not null default 'contributor'
                 check (role in ('owner', 'contributor', 'viewer')),
    added_at   timestamptz not null default now(),
    primary key (project_id, user_id)
);

-- Audit trail: who submitted what, where it was routed (append-only).
create table registry.submissions (
    submission_id   uuid primary key default gen_random_uuid(),
    project_id      uuid not null references registry.projects(project_id) on delete cascade,
    submitted_by    uuid references registry.users(user_id),
    experiment_hash text,                                       -- maps to triage.experiments in the project DB
    profile         text not null check (profile in ('local', 'cloud')),
    batch_job_id    text,                                       -- AWS Batch id (cloud), null for local
    submitted_at    timestamptz not null default now()
);
```

---

## 4. Per-project DB (`triage` schema)

### 4.1 Enums

```sql
create schema if not exists triage;

create type triage.problem_type as enum
    ('classification', 'regression_ranking', 'regression', 'survival');
create type triage.split_kind   as enum
    ('train', 'test', 'validation', 'production');
create type triage.run_status   as enum ('started', 'completed', 'failed');
create type triage.artifact_kind as enum
    ('cohort', 'labels', 'feature_group', 'matrix', 'model');
```

### 4.2 Lineage: experiments → runs → model_groups → models → matrices

```sql
create table triage.experiments (
    experiment_hash text primary key,                 -- deterministic content hash of the config
    config          jsonb not null,
    problem_type    triage.problem_type not null,
    created_at      timestamptz not null default now()
    -- dropped: time_splits/as_of_times/total_features/... → derive via a summary view if needed
);

create table triage.runs (
    run_id          uuid primary key default gen_random_uuid(),
    experiment_hash text references triage.experiments(experiment_hash) on delete cascade,
    profile         text not null check (profile in ('local', 'cloud')),
    status          triage.run_status not null default 'started',
    started_at      timestamptz not null default now(),
    finished_at     timestamptz,
    triage_version  text,
    git_hash        text,
    random_seed     bigint,
    batch_job_id    text,                             -- cloud profile
    error           text                              -- stacktrace if failed
    -- dropped: ec2_instance_type/os_user/working_directory/log_location/installed_libraries
    --          → execution-env detail lives in logs + the Batch/adapter layer, not results
);

create table triage.model_groups (
    model_group_id   bigint generated always as identity primary key,
    model_group_hash text  not null unique,           -- deterministic → upsert, replaces the stored proc
    model_type       text  not null,
    hyperparameters  jsonb not null,
    feature_list     text[] not null,
    config           jsonb,
    created_at       timestamptz not null default now()
);

create table triage.matrices (
    matrix_uuid    uuid primary key,                  -- = uuid5(artifact_id) (ADR-0015)
    artifact_id    text references triage.artifacts(artifact_id) on delete set null,
    matrix_kind    triage.split_kind not null,
    storage_uri    text not null,                     -- s3://… or file://… (Parquet pointer, ADR-0005)
    storage_format text not null default 'parquet',
    num_entities   integer,
    num_features   integer,
    feature_names  text[],
    label_timespan interval,
    lookback       interval,
    metadata       jsonb,
    built_by_run   uuid references triage.runs(run_id) on delete set null,
    created_at     timestamptz not null default now()
);

create table triage.models (
    model_id                bigint generated always as identity primary key,
    model_group_id          bigint not null references triage.model_groups(model_group_id) on delete cascade,
    model_hash              text   not null unique,    -- = artifacts.artifact_id of the model node (ADR-0015)
    run_id                  uuid   references triage.runs(run_id) on delete set null,
    train_matrix_uuid       uuid   references triage.matrices(matrix_uuid),
    train_end_time          date,
    training_label_timespan interval,
    artifact_uri            text,                      -- s3://…/model.joblib or file://… (ADR-0005)
    artifact_format         text default 'joblib',
    model_size_bytes        bigint,
    random_seed             bigint,
    created_at              timestamptz not null default now()
    -- model_type/hyperparameters NOT duplicated here — they live on model_group
);
```

### 4.3 Cohort + labels (survival-ready — ADR-0010)

> Both tables are populated by the adapter from **templated user SQL** (`{as_of_date}`, `{label_timespan}`), driven by **timechop**'s split dates; the label query's required columns are dictated by `problem_type` (`outcome` | `duration, event_observed`). (Q5)

```sql
create table triage.cohorts (
    cohort_hash text   not null,                       -- artifact_id of the cohort@(config, date) node (ADR-0015)
    entity_id   bigint not null,
    as_of_date  date   not null,
    primary key (cohort_hash, entity_id, as_of_date)
);

-- Realized ground truth per (entity, as_of_date). Survival-ready from day one.
-- label_hash discriminates label definitions (ADR-0015) — without it, two
-- different label queries would collide in the table.
create table triage.labels (
    label_hash     text     not null,                  -- artifact_id of the labels@(config, date, timespan) node
    entity_id      bigint   not null,
    as_of_date     date     not null,
    label_timespan interval not null,
    outcome        double precision,                   -- classification 0/1 OR regression target
    duration       double precision,                   -- survival: time-to-event-or-censoring (nullable)
    event_observed boolean,                            -- survival: observed vs censored   (nullable)
    created_at     timestamptz not null default now(),
    primary key (label_hash, entity_id, as_of_date, label_timespan)
);
```

### 4.4 Predictions (append-only, partitioned, survival-ready — ADR-0006, 0010)

```sql
create table triage.predictions (
    prediction_id  bigint generated always as identity,
    model_id       bigint not null references triage.models(model_id) on delete cascade,
    entity_id      bigint not null,
    as_of_date     date   not null,
    split_kind     triage.split_kind not null,         -- replaces the 3 duplicate tables
    scored_at      timestamptz not null default now(),  -- APPEND-ONLY: history, never overwritten
    score          double precision not null,           -- probability | value | risk (not Numeric(6,5))
    matrix_uuid    uuid references triage.matrices(matrix_uuid),
    primary key (prediction_id, scored_at)              -- partition key must be in the PK
) partition by range (scored_at);

-- e.g. quarterly partitions; created by the app or pg_partman.
create table triage.predictions_2026q2 partition of triage.predictions
    for values from ('2026-04-01') to ('2026-07-01');

create index on triage.predictions (model_id, as_of_date, scored_at);
create index on triage.predictions (entity_id);
```

**Ranks are not stored** — `precision@k` / top-k lists are computed in views with window functions (§5), removing the 4 old rank columns and all staleness. The realized label is **joined from `triage.labels`** at eval/read time rather than denormalized into every prediction row (flag in §8 if you want the denormalization for read speed).

### 4.5 Evaluation, subsets + bias (in-PG, problem_type-organized — ADR-0007)

```sql
-- Subset definitions: evaluate a model on a SQL-defined slice of the cohort (Q6).
-- Table kept now (schema-complete); the subset-evaluation FEATURE is deferred to post-v1.
create table triage.subsets (
    subset_hash text  primary key,
    config      jsonb not null,
    created_at  timestamptz not null default now()
);

-- One row per (model, split, as_of_date, subset, metric, parameter). Metric names span all problem_types.
create table triage.evaluations (
    model_id    bigint not null references triage.models(model_id) on delete cascade,
    split_kind  triage.split_kind not null,
    as_of_date  date   not null,
    subset_hash text   not null default '',
    metric      text   not null,        -- 'precision@','recall@','auc_roc','rmse','mae','r2','concordance_index'
    parameter   text   not null default '',  -- 'top_k=100' | 'pct=10' | '' (scalar)
    value          double precision,     -- deterministic-tiebreak realization (the metric for the actual list)
    value_worst    double precision,     -- exact analytic worst-case bound (threshold metrics only;
    value_best     double precision,     --   all four null for scalar metrics: auc_roc / rmse / c-index)
    value_expected double precision,     -- exact stochastic value = hypergeometric mean (Pa + d·P/T)/k
    value_std      double precision,     -- exact hypergeometric SD (confidence band on the metric)
    num_labeled integer,                 -- generic
    num_positive integer,                -- classification only (null otherwise)
    computed_at timestamptz not null default now(),
    primary key (model_id, split_kind, as_of_date, subset_hash, metric, parameter)
);

-- Protected attributes per (entity, as_of_date) — long-format, adapter-built from user SQL (Q4).
-- Kept SEPARATE from features so they can be excluded from the model yet used for audit.
create table triage.protected_groups (
    entity_id       bigint not null,
    as_of_date      date   not null,
    attribute_name  text   not null,   -- 'race', 'sex', 'age_bracket'
    attribute_value text   not null,   -- 'Black', 'F', '65+'
    primary key (entity_id, as_of_date, attribute_name)
);

-- Long-format bias metrics — replaces the 50-column aequitas dump; computed by SQL (ADR-0007).
create table triage.bias_metrics (
    model_id        bigint not null references triage.models(model_id) on delete cascade,
    split_kind      triage.split_kind not null,
    as_of_date      date   not null,
    parameter       text   not null default '',     -- threshold (top_k / pct)
    attribute_name  text   not null,                -- protected attribute, e.g. 'race'
    attribute_value text   not null,                -- group, e.g. 'Black'
    metric          text   not null,                -- 'fdr','fpr','tpr','precision','group_size',…
    value           double precision,
    ref_group_value text,                           -- reference group for disparity
    disparity       double precision,               -- value ÷ ref-group value
    computed_at     timestamptz not null default now(),
    primary key (model_id, split_kind, as_of_date, parameter,
                 attribute_name, attribute_value, metric)
);
```

### 4.6 Importances (persisted at train/predict time — ADR-0011)

```sql
create table triage.feature_importances (
    model_id           bigint not null references triage.models(model_id) on delete cascade,
    feature            text   not null,
    feature_importance double precision,
    rank_abs           integer,
    rank_pct           double precision,
    primary key (model_id, feature)
);

create table triage.individual_importances (
    model_id        bigint not null references triage.models(model_id) on delete cascade,
    entity_id       bigint not null,
    as_of_date      date   not null,
    feature         text   not null,
    method          text   not null,
    feature_value   double precision,
    importance_score double precision,
    primary key (model_id, entity_id, as_of_date, feature, method)
);
```

### 4.7 Source registry + pins (ADR-0013, ADR-0014)

> Declared input tables and their load versions. Pins enter derivation hashes
> (artifact identity, ADR-0013); the full design lives in
> `docs/derivation-dag.md`. Fingerprints are **advisory drift detection only**
> — never identity. A declared source with no pin is volatile: derivations
> touching it are never cache hits, with a loud warning.

```sql
create table triage.sources (
    source_name           text primary key,
    relation              text not null,     -- schema-qualified relation it points at
    knowledge_date_column text,              -- enables max() advisory fingerprint
    description           text,
    created_at            timestamptz not null default now()
);

create table triage.source_versions (
    source_name   text not null references triage.sources(source_name) on delete cascade,
    version_label text not null,
    registered_at timestamptz not null default now(),
    fingerprint   jsonb,                     -- advisory {row_count, max_knowledge_date}
    primary key (source_name, version_label)
);

-- Current pin = most recently registered version per source.
create view triage.current_source_pins as
select distinct on (source_name)
       source_name, version_label, registered_at, fingerprint
from   triage.source_versions
order  by source_name, registered_at desc;

-- Pins frozen at plan time for a run (the `guix describe` analog).
-- source_name intentionally has NO FK: this is an immutable historical record
-- that must capture declared-but-unregistered (volatile) sources too, and must
-- survive registry changes.
create table triage.run_source_pins (
    run_id        uuid not null references triage.runs(run_id) on delete cascade,
    source_name   text not null,
    version_label text,                      -- null = volatile (unpinned at plan time)
    fingerprint   jsonb,                     -- captured at build time (drift check)
    primary key (run_id, source_name)
);
```

### 4.8 Artifact DAG (ADR-0013, ADR-0015)

> One row per cached, materialized artifact; identity = derivation hash over
> the full input closure. Node grain per ADR-0015: cohort/labels/feature-group
> per as_of_date, matrices per split-side, models — and nothing above models
> (predictions are events, evaluations are recomputable SQL). Full design in
> `docs/derivation-dag.md` §4. FK hardening from domain tables is deferred to
> the GC pass. In the baseline migration this block is created **before**
> `matrices` so `matrices.artifact_id` can reference it.

```sql
create table triage.artifacts (
    artifact_id     text primary key,              -- derivation hash (ADR-0013)
    kind            triage.artifact_kind not null,
    cacheable       boolean not null default true, -- false: volatile inputs (ADR-0014)
    config          jsonb not null,                -- canonical own-config slice
    source_pins     jsonb not null default '{}'::jsonb,
    engine_versions jsonb not null default '{}'::jsonb,
    output_ref      text,                          -- table/date-slice, parquet URI, model URI
    status          text not null default 'building'
                      check (status in ('building', 'built', 'failed')),
    built_by_run    uuid references triage.runs(run_id) on delete set null,
    created_at      timestamptz not null default now(),
    built_at        timestamptz
);

create table triage.artifact_inputs (
    artifact_id text not null references triage.artifacts(artifact_id) on delete cascade,
    parent_id   text not null references triage.artifacts(artifact_id),
    primary key (artifact_id, parent_id)
);
create index artifact_inputs_parent_idx on triage.artifact_inputs (parent_id);
```

---

## 5. Representative views (the in-PG compute surface — ADR-0007)

```sql
-- Latest scoring run per (model, as_of_date), then ranked — feeds top-k lists + precision@k.
create view triage.latest_predictions as
select distinct on (model_id, entity_id, as_of_date)
       model_id, entity_id, as_of_date, split_kind, score, scored_at
from   triage.predictions
order  by model_id, entity_id, as_of_date, scored_at desc;

-- Prioritization list: rank entities for a model at an as_of_date (deterministic tiebreak).
create view triage.prediction_ranks as
select p.*,
       row_number()  over w as rank_abs,
       percent_rank() over w as rank_pct
from   triage.latest_predictions p
window w as (partition by model_id, as_of_date order by score desc, entity_id);

-- Leaderboard: best model per group by a chosen metric (a materialized view for the dashboard).
create materialized view triage.leaderboard as
select mg.model_group_id, mg.model_type,
       e.metric, e.parameter, e.as_of_date,
       e.value, m.model_id, m.train_end_time
from   triage.evaluations e
join   triage.models       m  using (model_id)
join   triage.model_groups mg using (model_group_id);
```

---

## 6. Old → new mapping

| Old | New |
|---|---|
| `triage_metadata` + `train_results` + `test_results` + `triage_production` | one `triage` schema per project DB |
| 3× `predictions` | one `triage.predictions` + `split_kind`, append-only + `scored_at`, partitioned |
| 2× `evaluations` | one `triage.evaluations` + `split_kind`, metric names span problem_types |
| 2× `aequitas` (50 cols) | `triage.bias_metrics` (long format, SQL-computed) |
| `label_value Integer` | `labels.outcome/duration/event_observed` + `predictions` label via join |
| stored-proc model-group identity | `model_groups.model_group_hash` (deterministic upsert) |
| file path implied by `matrix_uuid` | explicit `matrices.storage_uri` (Parquet, S3/local) |
| `triage_runs` (env detail) | `triage.runs` (lean; env detail → logs/adapters) |
| 4 stored rank columns | computed in `triage.prediction_ranks` view |
| `experiments.*_count` columns | dropped (derive via view) |
| `retrain` / `retrain_models` / `triage_production` schema | dropped — production = `predictions(split_kind='production')` |
| `aequitas` join needed protected attrs ad hoc | dedicated `triage.protected_groups` (long-format) |

---

## 7. "Safe / modern / clean" checklist

- **Safe:** every FK has an explicit `on delete` action (no more Python-side cascade); `check` constraints + `enum`s replace free strings; `not null` by default; no credentials in any table; per-project DB isolation means no cross-tenant leak surface.
- **Modern:** `jsonb`, `timestamptz`, `gen_random_uuid()`, `generated always as identity`, native enums, declarative range partitioning, `text[]`, window-function views, materialized leaderboard.
- **Clean:** 4 schemas → 1; 7 duplicated tables → 3 discriminated; 50-col bias dump → long format; dedup `model_type`/`hyperparameters`; drop derivable columns.

---

## 8. Resolved decisions (2026-06-05)

All six open questions are resolved; the DDL above reflects them.

1. **Labels — JOIN, not denormalize.** `predictions` carries no label; eval joins `predictions → labels`. Decided on *correctness*: in production/monitoring the outcome arrives **after** scoring, so denormalizing would force backfilling append-only rows. The join handles the experiment case (label already present) and the monitoring case (label arrives later) with one code path.
2. **Predictions partitioning — `scored_at`, quarterly, keep-forever default.** Append happens at the head (recent partition hot, old cold); eval-of-latest still prunes well; retention becomes a one-line `drop partition` when production volume demands it.
3. **Tie-handling — deterministic value + exact analytic stats, no stochastic trials.** `evaluations` stores `value` (deterministic tiebreak) + `value_worst`/`value_best`/`value_expected`/`value_std`, all computed analytically in one SQL pass. The stochastic value old triage *estimated* by Monte Carlo is the closed-form **hypergeometric mean** `(Pa + d·P/T)/k`, with hypergeometric SD — exact, no `sort_seed`/`num_sort_trials`. Threshold metrics only; null for scalar metrics.
4. **Protected attributes — dedicated long-format `triage.protected_groups`**, adapter-built from a user SQL query, kept separate from features (so they can be excluded from the model yet used for audit). Bias = pure SQL group-by.
5. **Cohort/label contract — timechop stays** as the as_of_date/split generator feeding featurizer; **templated SQL** (`{as_of_date}`, `{label_timespan}`); the label query's required columns are **dictated by `problem_type`** (`outcome` | `duration, event_observed`).
6. **Subsets & retrain.** Subsets: `triage.subsets` table kept now, the evaluation *feature* deferred (additive `WHERE` filter). Retrain: **no dedicated tables** — a retrained production model is just a `triage.models` row, its predictions are `predictions(split_kind='production')`; the workflow is deferred (ADR-0006). The old `retrain`/`retrain_models` tables and `triage_production` schema are dropped.
7. **Source-data pinning (added 2026-06-11, ADR-0014).** Source tables enter artifact identity as **declared registry pins** (`triage.sources` / `source_versions`, §4.7), frozen per run into `run_source_pins`; unpinned = volatile (never cached, loud warning); fingerprints advisory-only. Part of the broader derivation-DAG design (`docs/derivation-dag.md`, ADR-0013).
8. **DAG node granularity (added 2026-06-11, ADR-0015).** Data layer per as_of_date (features per adapter-defined **feature group** × date); matrices per split-side with the test matrix taking the train matrix as a parent (fitted imputation stats, ADR-0009); models last cached node — predictions/evaluations get no artifact rows. Derivation ids **replace** the inherited hashes: `model_hash` := artifact id, `matrix_uuid` := uuid5(artifact id), `cohort_hash` := per-date node id, and `labels.label_hash` joins the PK (fixes the missing label-definition discriminator). Remaining open: engine-version policy, GC/retention (which also decides FK hardening).

### Still deferred to the adapter-spec pass (not schema-blocking)

- The detailed timechop `temporal_config` shape (windows, frequencies).
- The featurizer ER-graph config section + how cohort rows become featurizer's target.
- Imputation policy wiring (ADR-0009: fit-free in featurizer, fit-based in the adapter).
