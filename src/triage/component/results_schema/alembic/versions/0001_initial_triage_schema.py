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

-- ---------------------------------------------------------------- lineage
create table triage.experiments (
    experiment_hash text primary key,
    config          jsonb not null,
    problem_type    triage.problem_type not null,
    created_at      timestamptz not null default now()
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

create table triage.matrices (
    matrix_uuid    uuid primary key,
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
    model_hash              text   not null unique,
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

-- --------------------------------------------- cohort + labels (survival-ready)
create table triage.cohorts (
    cohort_hash text   not null,
    entity_id   bigint not null,
    as_of_date  date   not null,
    primary key (cohort_hash, entity_id, as_of_date)
);

create table triage.labels (
    entity_id      bigint   not null,
    as_of_date     date     not null,
    label_timespan interval not null,
    outcome        double precision,
    duration       double precision,
    event_observed boolean,
    created_at     timestamptz not null default now(),
    primary key (entity_id, as_of_date, label_timespan)
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
    model_id      bigint not null references triage.models(model_id) on delete cascade,
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
