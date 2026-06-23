"""dashboard drill-downs (experiment actuals, ontology profile, entity profile)

Revision ID: 0006_dashboard_drilldowns
Revises: 0005_experiment_dashboard
Create Date: 2026-06-23

A follow-up to the 0005 experiment-scoped rework, adding the read SQL three review
findings need (raw SQL in ``op.execute``, mirroring 0004/0005):

* **experiment actuals** — ``experiment_actuals`` derives ``n_model_groups`` /
  ``n_models`` / ``n_splits`` / ``n_features`` / latest ``base_rate`` + ``cohort_size``
  straight from ``models ⋈ runs ⋈ matrices`` and the per-run profile views. These are the
  *built* shape (vs. ``runs.plan``, which is the forward-looking telemetry written at run
  start and is NULL for runs that predate it). ``experiment_summary`` LEFT JOINs it so the
  experiment list + overview strip are never blank on a completed experiment.
* **ontology profile** — ``source_profile(source)`` returns total rows, knowledge-date
  range, and distinct entities for a registered source relation (injection-safe ``regclass``
  + ``format(%I)``, same shape as ``source_volume``). Powers the richer Ontology screen.
* **entity profile** — ``entity_score_history(entity[, experiment])`` (score/rank trajectory
  across as_of_dates per model_group, from ``prediction_ranks``), ``entity_label_history``
  (outcome over time from ``labels``), and ``entity_attributes(entity)`` (the entity-grain
  source row as jsonb). A nullable ``triage.sources.role`` column marks the entity source
  explicitly; the lookup falls back to "registered source whose relation has an ``entity_id``
  column, fewest rows" when ``role`` is unset.
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "0006_dashboard_drilldowns"
down_revision = "0005_experiment_dashboard"
branch_labels = None
depends_on = None


UPGRADE_DDL = r"""
-- ------------------------------------------------------------- source role (entity vs event)
-- Display-only marker so the entity profile knows which registered source carries the
-- one-row-per-entity attributes (vs. the many-per-entity event stream). Nullable; the
-- entity_attributes() fallback works even when unset (older registrations).
alter table triage.sources add column if not exists role text
    check (role in ('entity', 'event'));

-- ------------------------------------------------------------- experiment actuals (built shape)
-- The *realized* counts, derived from what was actually built — independent of runs.plan
-- (forward-looking telemetry, NULL for runs predating the column). One row per experiment.
create or replace view triage.experiment_actuals as
with mdl as (
    select r.experiment_hash,
           count(distinct m.model_group_id) as n_model_groups,
           count(distinct m.model_id)       as n_models,
           count(distinct m.train_end_time) as n_splits,
           max(mx.num_features)             as n_features
    from   triage.models m
    join   triage.runs r on r.run_id = m.run_id
    left join triage.matrices mx on mx.matrix_uuid = m.train_matrix_uuid
    group by r.experiment_hash
),
-- latest base rate / cohort size for the experiment = the value at the most recent
-- as_of_date across all of the experiment's runs (the per-run profile views are 0004's).
br as (
    select r.experiment_hash, lbr.base_rate,
           row_number() over (partition by r.experiment_hash order by lbr.as_of_date desc) as rn
    from   triage.label_base_rate lbr
    join   triage.runs r on r.run_id = lbr.run_id
    where  lbr.base_rate is not null
),
ch as (
    select r.experiment_hash, cp.n_entities,
           row_number() over (partition by r.experiment_hash order by cp.as_of_date desc) as rn
    from   triage.cohort_profile cp
    join   triage.runs r on r.run_id = cp.run_id
)
select e.experiment_hash,
       coalesce(mdl.n_model_groups, 0) as n_model_groups,
       coalesce(mdl.n_models, 0)       as n_models,
       coalesce(mdl.n_splits, 0)       as n_splits,
       mdl.n_features                  as n_features,
       br.base_rate                    as base_rate,
       ch.n_entities                   as cohort_size
from   triage.experiments e
left join mdl on mdl.experiment_hash = e.experiment_hash
left join br  on br.experiment_hash  = e.experiment_hash and br.rn = 1
left join ch  on ch.experiment_hash  = e.experiment_hash and ch.rn = 1;

-- Re-create experiment_summary with the actuals appended (CREATE OR REPLACE keeps the
-- leading 10 columns identical to 0005 and adds the 6 actuals at the end).
create or replace view triage.experiment_summary as
with base as (
    select e.experiment_hash,
           coalesce(nullif(e.name, ''), triage.auto_experiment_name(e.experiment_hash)) as name,
           e.description, e.author, e.problem_type, e.created_at,
           count(r.run_id)                                          as n_runs,
           max(r.started_at)                                        as last_started_at,
           (array_agg(r.status order by r.started_at desc nulls last))[1] as last_status,
           (array_agg(r.plan   order by r.started_at desc nulls last))[1] as last_plan
    from   triage.experiments e
    left join triage.runs r using (experiment_hash)
    group by e.experiment_hash, e.name, e.description, e.author, e.problem_type, e.created_at
)
select base.experiment_hash, base.name, base.description, base.author, base.problem_type,
       base.created_at, base.n_runs, base.last_started_at, base.last_status, base.last_plan,
       a.n_model_groups, a.n_models, a.n_splits, a.n_features, a.base_rate, a.cohort_size
from   base
left join triage.experiment_actuals a using (experiment_hash);

-- ------------------------------------------------------------- ontology source profile
-- Total rows + knowledge-date range + distinct entities for a registered source relation.
-- Injection-safe: regclass-validates the relation, format(%I) quotes the date column;
-- distinct entities only when the relation actually has an entity_id column.
create or replace function triage.source_profile(p_source text)
returns table(total_rows bigint, first_date date, last_date date, n_distinct_entities bigint)
language plpgsql stable as $fn$
declare
    rel text;
    kd  text;
    has_entity boolean;
    date_lo text;
    date_hi text;
    ent text;
begin
    select s.relation, s.knowledge_date_column into rel, kd
    from triage.sources s where s.source_name = p_source;
    if rel is null then return; end if;

    select exists(
        select 1 from pg_attribute a
        where a.attrelid = rel::regclass and a.attname = 'entity_id'
          and a.attnum > 0 and not a.attisdropped
    ) into has_entity;

    date_lo := case when kd is null then 'null::date' else format('min(%I)::date', kd) end;
    date_hi := case when kd is null then 'null::date' else format('max(%I)::date', kd) end;
    ent     := case when has_entity then 'count(distinct entity_id)::bigint' else 'null::bigint' end;

    return query execute format(
        'select count(*)::bigint as total_rows, %s as first_date, %s as last_date,'
        ' %s as n_distinct_entities from %s',
        date_lo, date_hi, ent, rel::regclass::text);
end;
$fn$;

-- ------------------------------------------------------------- entity profile
-- Score/rank trajectory across as_of_dates per model_group (optionally one experiment).
-- NOT via prediction_ranks: that view ranks the WHOLE (append-only, re-run-inflated)
-- predictions table and only then filters by entity_id — entity_id isn't in the window
-- partition key, so a single-entity lookup ranked millions of rows (~100s). Instead: pull
-- the entity's own scores (cheap, predictions_entity_idx), then rank each within ONLY its
-- (model, as_of_date) partition via a lateral count over latest_predictions (~0.2s).
create or replace function triage.entity_score_history(p_entity bigint, p_experiment text default null)
returns table(model_group_id bigint, model_id bigint, experiment_hash text, as_of_date date,
              score double precision, rank_abs bigint, rank_pct double precision,
              model_type text, hyperparameters jsonb, train_end_time date)
language sql stable as $$
    with scope as (
        select m.model_id, m.model_group_id, m.train_end_time, r.experiment_hash,
               mg.model_type, mg.hyperparameters
        from   triage.models m
        join   triage.runs r on r.run_id = m.run_id
        join   triage.model_groups mg on mg.model_group_id = m.model_group_id
        where  (p_experiment is null or r.experiment_hash = p_experiment)
    ),
    mine as (
        select distinct on (p.model_id, p.as_of_date) p.model_id, p.as_of_date, p.score
        from   triage.predictions p
        where  p.entity_id = p_entity
        order  by p.model_id, p.as_of_date, p.scored_at desc
    )
    select s.model_group_id, s.model_id, s.experiment_hash, mine.as_of_date, mine.score,
           rk.rank_abs, rk.rank_pct, s.model_type, s.hyperparameters, s.train_end_time
    from   mine
    join   scope s on s.model_id = mine.model_id
    cross join lateral (
        select count(*) filter (where lp.score > mine.score
                                  or (lp.score = mine.score and lp.entity_id < p_entity)) + 1 as rank_abs,
               case when count(*) > 1
                    then (count(*) filter (where lp.score > mine.score))::double precision / (count(*) - 1)
                    else 0 end as rank_pct
        from   triage.latest_predictions lp
        where  lp.model_id = mine.model_id and lp.as_of_date = mine.as_of_date
    ) rk
    order  by s.model_group_id, mine.as_of_date;
$$;

-- Outcome history for the entity (deduped across label artifacts that share a config).
create or replace function triage.entity_label_history(p_entity bigint)
returns table(as_of_date date, label_timespan interval, outcome double precision)
language sql stable as $$
    select distinct l.as_of_date, l.label_timespan, l.outcome
    from   triage.labels l
    where  l.entity_id = p_entity
    order  by l.as_of_date, l.label_timespan;
$$;

-- The entity-grain source row as jsonb. Prefer the source flagged role='entity'; else the
-- registered source whose relation has an entity_id column with the fewest rows (entities
-- are one-per-id, events many-per-id). Latest row by knowledge_date when available.
create or replace function triage.entity_attributes(p_entity bigint)
returns jsonb
language plpgsql stable as $fn$
declare
    rel text;
    kd  text;
    result jsonb;
begin
    select s.relation, s.knowledge_date_column into rel, kd
    from triage.sources s where s.role = 'entity' limit 1;

    if rel is null then
        select s.relation, s.knowledge_date_column into rel, kd
        from triage.sources s
        where exists (
            select 1 from pg_attribute a
            where a.attrelid = s.relation::regclass and a.attname = 'entity_id'
              and a.attnum > 0 and not a.attisdropped)
        order by (select c.reltuples from pg_class c where c.oid = s.relation::regclass) asc
        limit 1;
    end if;

    if rel is null then return null; end if;

    execute format(
        'select to_jsonb(t) from %s t where t.entity_id = $1 %s limit 1',
        rel::regclass::text,
        case when kd is null then '' else format('order by %I desc', kd) end
    ) into result using p_entity;
    return result;
end;
$fn$;
"""


DOWNGRADE_DDL = r"""
drop function if exists triage.entity_attributes(bigint);
drop function if exists triage.entity_label_history(bigint);
drop function if exists triage.entity_score_history(bigint, text);
drop function if exists triage.source_profile(text);

-- restore the 0005 experiment_summary (drop the actuals columns by dropping + recreating)
drop view if exists triage.experiment_summary;
drop view if exists triage.experiment_actuals;
create or replace view triage.experiment_summary as
select e.experiment_hash,
       coalesce(nullif(e.name, ''), triage.auto_experiment_name(e.experiment_hash)) as name,
       e.description, e.author, e.problem_type, e.created_at,
       count(r.run_id)                                          as n_runs,
       max(r.started_at)                                        as last_started_at,
       (array_agg(r.status order by r.started_at desc nulls last))[1] as last_status,
       (array_agg(r.plan   order by r.started_at desc nulls last))[1] as last_plan
from   triage.experiments e
left join triage.runs r using (experiment_hash)
group by e.experiment_hash, e.name, e.description, e.author, e.problem_type, e.created_at;

alter table triage.sources drop column if exists role;
"""


def upgrade():
    op.execute(UPGRADE_DDL)


def downgrade():
    op.execute(DOWNGRADE_DDL)
