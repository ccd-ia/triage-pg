"""entity profile: readable geo coords + full as_of label grid; archived experiments

Revision ID: 0008_entity_geo_labels
Revises: 0007_rayid_curve_labeled
Create Date: 2026-06-26

Three review findings on the entity profile + experiment list:

* **geo attributes** — ``entity_attributes(entity)`` returned the entity-grain row via a bare
  ``to_jsonb(t)``, so a PostGIS ``geography``/``geometry`` column (DirtyDuck ``location``) showed
  up as a raw WKB hex string (``0101000020E61...``). Rewrite the dynamic SQL to override each geo
  column with ``{lon, lat, geojson, kind:'geo'}`` (``ST_X/ST_Y`` of the centroid + ``ST_AsGeoJSON``)
  so the dashboard can render readable coordinates + an OpenStreetMap link. Degrades to the plain
  row when PostGIS is absent (local profile, ADR-0003): no geography/geometry columns exist, so the
  override loop never runs and no ``ST_*`` reference is generated.

* **label history** — ``entity_label_history(entity)`` returned only the rows that exist in
  ``triage.labels`` (``select distinct``), so as_of dates where the entity is in the cohort but has
  no matured label were hidden. The user expects to see the full grid with those rows as NULL.
  Rewrite to enumerate the entity's cohort memberships crossed with the label timespan(s) and
  LEFT JOIN labels, so missing outcomes surface as NULL. Signature is unchanged.

* **archived experiments** — expose ``experiments.archived_at`` through ``experiment_summary`` so
  the dashboard ``/experiments`` list can hide soft-archived experiments (e.g. a stale pre-fix
  duplicate) while ``/experiments/{hash}`` stays reachable by direct link. CREATE OR REPLACE appends
  the column at the end, keeping the leading 16 columns identical to 0006.
"""

from alembic import op

# revision identifiers, used by Alembic. (id <= 32 chars: results_schema_versions.version_num)
revision = "0008_entity_geo_labels"
down_revision = "0007_rayid_curve_labeled"
branch_labels = None
depends_on = None


UPGRADE_DDL = r"""
-- ------------------------------------------------------------- entity attributes (geo-aware)
-- The entity-grain source row as jsonb, but with PostGIS geography/geometry columns rendered as
-- readable coordinates instead of WKB hex. ST_Centroid makes lon/lat robust for non-point
-- geometries too (centroid of a point is the point). When PostGIS is absent the geo loop finds
-- no columns, so the function falls back to the plain to_jsonb(t) row (no ST_* is emitted).
create or replace function triage.entity_attributes(p_entity bigint)
returns jsonb
language plpgsql stable as $fn$
declare
    rel text;
    kd  text;
    result jsonb;
    geo_override text := '';
    grec record;
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

    for grec in
        select a.attname as col
        from   pg_attribute a
        join   pg_type ty on ty.oid = a.atttypid
        where  a.attrelid = rel::regclass and a.attnum > 0 and not a.attisdropped
          and  ty.typname in ('geography', 'geometry')
    loop
        geo_override := geo_override || format(
            ' || jsonb_build_object(%L, case when t.%I is null then null else jsonb_build_object('
            || '''lon'', ST_X(ST_Centroid(t.%I::geometry)),'
            || '''lat'', ST_Y(ST_Centroid(t.%I::geometry)),'
            || '''geojson'', ST_AsGeoJSON(t.%I)::jsonb,'
            || '''kind'', ''geo'') end)',
            grec.col, grec.col, grec.col, grec.col, grec.col);
    end loop;

    execute format(
        'select to_jsonb(t) %s from %s t where t.entity_id = $1 %s limit 1',
        geo_override,
        rel::regclass::text,
        case when kd is null then '' else format('order by %I desc', kd) end
    ) into result using p_entity;
    return result;
end;
$fn$;

-- ------------------------------------------------------------- entity label history (full grid)
-- Every as_of_date the entity is in the cohort, crossed with the label timespan(s), LEFT JOIN
-- labels: an as_of date with no matured outcome shows as NULL instead of being dropped. Picks the
-- latest label artifact's outcome for each (as_of_date, label_timespan).
create or replace function triage.entity_label_history(p_entity bigint)
returns table(as_of_date date, label_timespan interval, outcome double precision)
language sql stable as $$
    with grid as (
        select distinct c.as_of_date from triage.cohorts c where c.entity_id = p_entity
    ),
    spans as (
        select distinct l.label_timespan from triage.labels l
    )
    select g.as_of_date, sp.label_timespan,
           (select l.outcome
            from   triage.labels l
            where  l.entity_id = p_entity
              and  l.as_of_date = g.as_of_date
              and  l.label_timespan = sp.label_timespan
            order by l.created_at desc
            limit 1) as outcome
    from   grid g
    cross join spans sp
    order by g.as_of_date, sp.label_timespan;
$$;

-- ------------------------------------------------------------- experiment_summary + archived_at
-- Append archived_at so the dashboard list can hide soft-archived experiments. Leading 16
-- columns are byte-identical to 0006; CREATE OR REPLACE only adds the trailing column.
create or replace view triage.experiment_summary as
with base as (
    select e.experiment_hash,
           coalesce(nullif(e.name, ''), triage.auto_experiment_name(e.experiment_hash)) as name,
           e.description, e.author, e.problem_type, e.created_at,
           count(r.run_id)                                          as n_runs,
           max(r.started_at)                                        as last_started_at,
           (array_agg(r.status order by r.started_at desc nulls last))[1] as last_status,
           (array_agg(r.plan   order by r.started_at desc nulls last))[1] as last_plan,
           e.archived_at
    from   triage.experiments e
    left join triage.runs r using (experiment_hash)
    group by e.experiment_hash, e.name, e.description, e.author, e.problem_type, e.created_at,
             e.archived_at
)
select base.experiment_hash, base.name, base.description, base.author, base.problem_type,
       base.created_at, base.n_runs, base.last_started_at, base.last_status, base.last_plan,
       a.n_model_groups, a.n_models, a.n_splits, a.n_features, a.base_rate, a.cohort_size,
       base.archived_at
from   base
left join triage.experiment_actuals a using (experiment_hash);
"""


# Restore the 0006/0007 bodies verbatim on downgrade.
DOWNGRADE_DDL = r"""
create or replace function triage.entity_label_history(p_entity bigint)
returns table(as_of_date date, label_timespan interval, outcome double precision)
language sql stable as $$
    select distinct l.as_of_date, l.label_timespan, l.outcome
    from   triage.labels l
    where  l.entity_id = p_entity
    order  by l.as_of_date, l.label_timespan;
$$;

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

-- restore the 0006 experiment_summary (without archived_at). DROP first: CREATE OR REPLACE
-- cannot remove a column, and nothing in the DB depends on this view (app-level reads only).
drop view if exists triage.experiment_summary;
create view triage.experiment_summary as
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
"""


def upgrade():
    op.execute(UPGRADE_DDL)


def downgrade():
    op.execute(DOWNGRADE_DDL)
