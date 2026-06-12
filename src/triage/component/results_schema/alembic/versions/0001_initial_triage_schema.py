"""initial triage-pg per-project schema (greenfield baseline)

Revision ID: 0001_initial_triage_schema
Revises:
Create Date: 2026-06-05

Greenfield baseline for a *per-project* results database (ADR-0001, ADR-0002):
creates the ``triage`` schema exactly as specified in ``docs/schema-design.md``.

The registry database schema (projects / users / routing) is a separate, later
migration — multi-project is post-v1 (ADR sequencing).

Written as raw SQL on purpose: declarative partitioning, ``generated always as
identity``, enums, materialized views, and check constraints do not round-trip
cleanly through SQLAlchemy autogenerate. Requires PostgreSQL >= 13
(``gen_random_uuid()`` is built in; identity + default partitions need >= 11).
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "0001_initial_triage_schema"
down_revision = None
branch_labels = None
depends_on = None


SCHEMA_DDL = r"""
create schema if not exists triage;

-- ---------------------------------------------------------------- enums
create type triage.problem_type as enum
    ('classification', 'regression_ranking', 'regression', 'survival');
create type triage.split_kind as enum
    ('train', 'test', 'validation', 'production');
create type triage.run_status as enum ('started', 'completed', 'failed');
create type triage.artifact_kind as enum
    ('cohort', 'labels', 'feature_group', 'matrix', 'model');

-- ---------------------------------------------------------------- lineage
create table triage.experiments (
    experiment_hash text primary key,
    config          jsonb not null,
    problem_type    triage.problem_type not null,
    created_at      timestamptz not null default now(),
    archived_at     timestamptz                    -- soft archive = GC root removal (ADR-0017)
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
    batch_job_id    text,
    error           text
);

create table triage.model_groups (
    model_group_id   bigint generated always as identity primary key,
    model_group_hash text   not null unique,
    model_type       text   not null,
    hyperparameters  jsonb  not null,
    feature_list     text[] not null,
    config           jsonb,
    created_at       timestamptz not null default now()
);

-- ------------------------------- artifact DAG (ADR-0013, ADR-0015)
-- One row per cached, materialized artifact; identity = derivation hash over
-- the full input closure. The DAG stops at models: predictions are append-only
-- events with native lineage (ADR-0006), evaluations are recomputable SQL
-- (ADR-0007). FK hardening from domain tables is deferred to the GC pass.
create table triage.artifacts (
    artifact_id     text primary key,              -- strict derivation hash (ADR-0013, ADR-0016)
    logical_id      text not null,                 -- engine-version-free hash chain (ADR-0016 fallback)
    kind            triage.artifact_kind not null,
    cacheable       boolean not null default true, -- false: volatile inputs (ADR-0014)
    config          jsonb not null,                -- canonical own-config slice
    source_pins     jsonb not null default '{}'::jsonb,
    engine_versions jsonb not null default '{}'::jsonb,
    output_ref      text,                          -- table/date-slice, parquet URI, model URI
    -- 'collected' = output GC'd, row kept for provenance; rebuilds on demand (ADR-0017)
    status          text not null default 'building'
                      check (status in ('building', 'built', 'failed', 'collected')),
    built_by_run    uuid references triage.runs(run_id) on delete set null,
    created_at      timestamptz not null default now(),
    built_at        timestamptz
);
create index artifacts_logical_idx on triage.artifacts (logical_id);

create table triage.artifact_inputs (
    artifact_id text not null references triage.artifacts(artifact_id) on delete cascade,
    -- RESTRICT: an edge is the child's provenance — a parent row cannot be
    -- purged while any child still references it; purge deletes bottom-up (ADR-0017).
    parent_id   text not null references triage.artifacts(artifact_id) on delete restrict,
    primary key (artifact_id, parent_id)
);
create index artifact_inputs_parent_idx on triage.artifact_inputs (parent_id);

-- Usage edges: every artifact a run touched, built OR cache-hit. GC roots are
-- computed from these — built_by_run alone is not a liveness edge, because a
-- later run can depend on an artifact it did not build (ADR-0017).
create table triage.run_artifacts (
    run_id      uuid not null references triage.runs(run_id) on delete cascade,
    artifact_id text not null references triage.artifacts(artifact_id) on delete cascade,
    used_at     timestamptz not null default now(),
    primary key (run_id, artifact_id)
);
create index run_artifacts_artifact_idx on triage.run_artifacts (artifact_id);

create table triage.matrices (
    matrix_uuid    uuid primary key,               -- = uuid5(artifact_id) (ADR-0015)
    artifact_id    text references triage.artifacts(artifact_id) on delete cascade,
    matrix_kind    triage.split_kind not null,
    storage_uri    text not null,
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
    model_hash              text   not null unique
                              references triage.artifacts(artifact_id) on delete cascade,
                            -- = artifacts.artifact_id of the model node (ADR-0015)
    run_id                  uuid   references triage.runs(run_id) on delete set null,
    train_matrix_uuid       uuid   references triage.matrices(matrix_uuid),
    train_end_time          date,
    training_label_timespan interval,
    artifact_uri            text,
    artifact_format         text default 'joblib',
    model_size_bytes        bigint,
    random_seed             bigint,
    created_at              timestamptz not null default now()
);

-- ------------------------------------- source registry + pins (ADR-0014)
-- Declared input tables and their load versions. Pins enter derivation
-- hashes (ADR-0013); fingerprints are advisory drift detection only.
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

-- --------------------------------------------- cohort + labels (survival-ready)
create table triage.cohorts (
    cohort_hash text   not null                    -- artifact_id of the cohort@(config, date) node (ADR-0015)
                  references triage.artifacts(artifact_id) on delete cascade,
    entity_id   bigint not null,
    as_of_date  date   not null,
    primary key (cohort_hash, entity_id, as_of_date)
);

create table triage.labels (
    label_hash     text     not null               -- artifact_id of the labels@(config, date, timespan) node (ADR-0015)
                     references triage.artifacts(artifact_id) on delete cascade,
    entity_id      bigint   not null,
    as_of_date     date     not null,
    label_timespan interval not null,
    outcome        double precision,
    duration       double precision,
    event_observed boolean,
    created_at     timestamptz not null default now(),
    primary key (label_hash, entity_id, as_of_date, label_timespan)
);

create table triage.protected_groups (
    entity_id       bigint not null,
    as_of_date      date   not null,
    attribute_name  text   not null,
    attribute_value text   not null,
    primary key (entity_id, as_of_date, attribute_name)
);

-- --------------------------------------- predictions (append-only, partitioned)
create table triage.predictions (
    prediction_id bigint generated always as identity,
    -- RESTRICT: predictions are append-only history (ADR-0006); deleting a
    -- predicted model must fail loudly, never silently eat its predictions (ADR-0017).
    model_id      bigint not null references triage.models(model_id) on delete restrict,
    entity_id     bigint not null,
    as_of_date    date   not null,
    split_kind    triage.split_kind not null,
    scored_at     timestamptz not null default now(),
    score         double precision not null,
    matrix_uuid   uuid references triage.matrices(matrix_uuid),
    primary key (prediction_id, scored_at)
) partition by range (scored_at);

-- MVP safety net so inserts always land somewhere; production adds quarterly
-- partitions (pg_partman / app) and migrates rows out of the default.
create table triage.predictions_default partition of triage.predictions default;

create index predictions_model_date_idx on triage.predictions (model_id, as_of_date, scored_at);
create index predictions_entity_idx     on triage.predictions (entity_id);

-- ---------------------------------------------- subsets + evaluation + bias
create table triage.subsets (
    subset_hash text  primary key,
    config      jsonb not null,
    created_at  timestamptz not null default now()
);

create table triage.evaluations (
    model_id       bigint not null references triage.models(model_id) on delete cascade,
    split_kind     triage.split_kind not null,
    as_of_date     date   not null,
    subset_hash    text   not null default '',
    metric         text   not null,
    parameter      text   not null default '',
    value          double precision,
    value_worst    double precision,
    value_best     double precision,
    value_expected double precision,
    value_std      double precision,
    num_labeled    integer,
    num_positive   integer,
    computed_at    timestamptz not null default now(),
    primary key (model_id, split_kind, as_of_date, subset_hash, metric, parameter)
);

create table triage.bias_metrics (
    model_id        bigint not null references triage.models(model_id) on delete cascade,
    split_kind      triage.split_kind not null,
    as_of_date      date   not null,
    parameter       text   not null default '',
    attribute_name  text   not null,
    attribute_value text   not null,
    metric          text   not null,
    value           double precision,
    ref_group_value text,
    disparity       double precision,
    computed_at     timestamptz not null default now(),
    primary key (model_id, split_kind, as_of_date, parameter,
                 attribute_name, attribute_value, metric)
);

-- -------------------------------- importances (persisted at train/predict time)
create table triage.feature_importances (
    model_id           bigint not null references triage.models(model_id) on delete cascade,
    feature            text   not null,
    feature_importance double precision,
    rank_abs           integer,
    rank_pct           double precision,
    primary key (model_id, feature)
);

create table triage.individual_importances (
    model_id         bigint not null references triage.models(model_id) on delete cascade,
    entity_id        bigint not null,
    as_of_date       date   not null,
    feature          text   not null,
    method           text   not null,
    feature_value    double precision,
    importance_score double precision,
    primary key (model_id, entity_id, as_of_date, feature, method)
);

-- ----------------------------------------- views (in-PG compute surface, ADR-0007)
-- Latest scoring run per (model, entity, as_of_date).
create view triage.latest_predictions as
select distinct on (model_id, entity_id, as_of_date)
       model_id, entity_id, as_of_date, split_kind, score, scored_at
from   triage.predictions
order  by model_id, entity_id, as_of_date, scored_at desc;

-- Prioritization list: deterministic-tiebreak ranks over the latest scores.
create view triage.prediction_ranks as
select p.*,
       row_number()   over w as rank_abs,
       percent_rank() over w as rank_pct
from   triage.latest_predictions p
window w as (partition by model_id, as_of_date order by score desc, entity_id);

-- Leaderboard for dashboards; refresh after evaluations land.
create materialized view triage.leaderboard as
select mg.model_group_id, mg.model_type,
       e.metric, e.parameter, e.as_of_date,
       e.value, e.value_expected, e.value_std,
       m.model_id, m.train_end_time
from   triage.evaluations e
join   triage.models       m  using (model_id)
join   triage.model_groups mg using (model_group_id)
with no data;
"""


def upgrade():
    op.execute(SCHEMA_DDL)


def downgrade():
    # The schema owns its enums and views; cascade drops everything.
    op.execute("drop schema if exists triage cascade;")
