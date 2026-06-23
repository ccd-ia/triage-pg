"""experiment-scoped dashboard SQL (dashboard rework — Experiment ▸ Model Group ▸ Model)

Revision ID: 0005_experiment_dashboard
Revises: 0004_dashboard_reads
Create Date: 2026-06-22

The read dashboard is being reworked from run-centric to the real hierarchy
**Experiment ▸ Model Group ▸ Model**, with analysis (audition / bias / leaderboard /
model-groups) scoped to the **experiment**, not a single run. This is a correctness fix,
not a preference: the derivation DAG cache-shares models across runs (on a cache-hit
``models.run_id`` stays the *original* builder run), so run-scoped audition is empty on a
re-run. Correct scope: ``models ⋈ runs WHERE runs.experiment_hash = :hash``.

This migration (mirroring the 0004 style — raw SQL in ``op.execute``):

* **re-scopes the audition layer to the experiment.** ``audition_distances`` /
  ``audition`` / ``audition_pick`` / ``latest_model`` / ``selected_model`` and the
  ``leaderboard`` matview now key on ``experiment_hash`` (the 0004 run-scoped versions are
  replaced; the per-run views ``run_summary`` / ``cohort_profile`` / ``label_base_rate`` /
  ``run_progress`` are legitimately per-run and untouched).
* **experiment identity** — ``triage.experiments.name`` / ``.description`` / ``.author``
  (cosmetic, *outside* the ``experiment_hash``) + ``auto_experiment_name(hash)`` (a
  deterministic mineral+scientist[+number] name) + ``experiment_summary``.
* **hierarchy reads** — ``model_group_summary``, ``metric_catalog``, ``artifact_sharing``
  (project-wide derivation: how many experiments/runs share a node).
* **model detail** — ``model_threshold_curve(model)`` (the classic Rayid precision/recall +
  TP/FP/FN/TN per population-cut, for the client k-slider) and
  ``model_score_histogram(model, bins)``.
* **ontology profile** — ``source_volume(source, grain)`` profiles a registered source
  relation's volume over time via its ``knowledge_date_column`` (injection-safe ``regclass``
  + ``format(%I)``), so the per-project ontology view needs no hardcoded ``ontology.*`` names.
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "0005_experiment_dashboard"
down_revision = "0004_dashboard_reads"
branch_labels = None
depends_on = None


UPGRADE_DDL = r"""
-- ------------------------------------------------------- experiment identity (cosmetic)
-- name/description/author are display-only and MUST NOT enter experiment_hash (the CLI
-- strips name/description from config before hashing; author = OS user). They live on the
-- experiments row, populated at experiment creation, never folded into identity.
alter table triage.experiments add column if not exists name text;
alter table triage.experiments add column if not exists description text;
alter table triage.experiments add column if not exists author text;

-- Deterministic auto-name from the hash (mineral + scientist/philosopher + number), so an
-- experiment without an explicit name still gets a stable, human label (Q2). Pure function
-- of the hash — same experiment -> same name, no storage needed.
create or replace function triage.auto_experiment_name(p_hash text)
returns text language sql immutable as $$
  with w as (select
    array['Quartz','Basalt','Cobalt','Pyrite','Marble','Onyx','Jade','Amber',
          'Slate','Opal','Granite','Gypsum','Cinnabar','Galena','Beryl','Topaz'] as minerals,
    array['Curie','Hypatia','Turing','Noether','Lovelace','Darwin','Bohr','Planck',
          'Hopper','Ramanujan','Galois','Euler','Kepler','Faraday','Mendel','Pasteur'] as people)
  select w.minerals[1 + (abs(hashtextextended(p_hash, 0)) % 16)] || '-'
      || w.people[1 + (abs(hashtextextended(p_hash, 1)) % 16)] || '-'
      || (1 + (abs(hashtextextended(p_hash, 2)) % 97))::text
  from w;
$$;

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

-- ----------------------------------------------- audition re-scoped to the experiment
-- Replace the 0004 run-scoped audition layer. Each (experiment, model_group, split) has
-- exactly one model (re-runs cache-hit -> one models row per model_hash), so aggregating by
-- experiment_hash is well-defined and survives a re-run (the Q1 fix).
drop view if exists triage.audition;
drop view if exists triage.audition_distances;

create view triage.audition_distances as
select r.experiment_hash, m.model_group_id, e.metric, e.parameter, e.as_of_date,
       m.train_end_time, e.value as raw_value,
       case when triage.higher_is_better(e.metric)
            then max(e.value) over w else min(e.value) over w end as best_value,
       abs((case when triage.higher_is_better(e.metric)
                 then max(e.value) over w else min(e.value) over w end) - e.value)
         as dist_from_best_case
from   triage.evaluations e
join   triage.models      m on m.model_id = e.model_id
join   triage.runs        r on r.run_id   = m.run_id
where  e.split_kind = 'test' and e.value is not null
window w as (partition by r.experiment_hash, e.metric, e.parameter, e.as_of_date);

create view triage.audition as
select experiment_hash, metric, parameter, model_group_id,
       count(*)                  as n_splits_evaluated,
       avg(raw_value)            as avg_value,
       stddev_samp(raw_value)    as stddev_value,
       avg(dist_from_best_case)  as avg_distance_from_best,
       max(dist_from_best_case)  as max_regret
from   triage.audition_distances
group by experiment_hash, metric, parameter, model_group_id;

-- The standard selection-rule catalog, re-scoped to experiment_hash (body identical to 0004
-- except the scope predicate ad.run_id=p_run -> ad.experiment_hash=p_experiment).
drop function if exists triage.audition_pick(uuid, text, text, text, jsonb);
create or replace function triage.audition_pick(
    p_experiment text, p_metric text, p_parameter text default '',
    p_rule text default 'best_current_value', p_params jsonb default '{}'::jsonb)
returns bigint language plpgsql stable as $fn$
declare
    hib boolean := triage.higher_is_better(p_metric);
    result bigint;
begin
    if p_rule = 'best_current_value' then
        select ad.model_group_id into result
        from triage.audition_distances ad
        where ad.experiment_hash = p_experiment and ad.metric = p_metric
          and ad.parameter = p_parameter
          and ad.as_of_date = (select max(as_of_date) from triage.audition_distances
                               where experiment_hash = p_experiment and metric = p_metric
                                 and parameter = p_parameter)
        order by case when hib then ad.raw_value end desc nulls last,
                 case when hib then null else ad.raw_value end asc,
                 ad.model_group_id
        limit 1;

    elsif p_rule = 'best_average_value' then
        select a.model_group_id into result from triage.audition a
        where a.experiment_hash = p_experiment and a.metric = p_metric
          and a.parameter = p_parameter
        order by case when hib then a.avg_value end desc nulls last,
                 case when hib then null else a.avg_value end asc,
                 a.model_group_id
        limit 1;

    elsif p_rule = 'lowest_metric_variance' then
        select a.model_group_id into result from triage.audition a
        where a.experiment_hash = p_experiment and a.metric = p_metric
          and a.parameter = p_parameter
        order by a.stddev_value asc nulls last, a.model_group_id
        limit 1;

    elsif p_rule = 'most_frequent_best_dist' then
        select ad.model_group_id into result
        from triage.audition_distances ad
        where ad.experiment_hash = p_experiment and ad.metric = p_metric
          and ad.parameter = p_parameter
        group by ad.model_group_id
        order by avg((ad.dist_from_best_case
                      <= (p_params->>'dist_window')::double precision)::int) desc,
                 ad.model_group_id
        limit 1;

    elsif p_rule = 'best_avg_var_penalized' then
        with g as (
            select ad.model_group_id, avg(ad.raw_value) as raw_avg,
                   coalesce(stddev_samp(ad.raw_value), 0) as raw_std
            from triage.audition_distances ad
            where ad.experiment_hash = p_experiment and ad.metric = p_metric
              and ad.parameter = p_parameter
            group by ad.model_group_id),
        mm as (select min(raw_std) as min_std from g),
        pen as (
            select g.model_group_id,
                   g.raw_avg - (case when hib then 1 else -1 end)
                       * (p_params->>'stdev_penalty')::double precision
                       * (g.raw_std - mm.min_std) as score
            from g cross join mm)
        select pen.model_group_id into result from pen
        order by case when hib then pen.score end desc nulls last,
                 case when hib then null else pen.score end asc,
                 pen.model_group_id
        limit 1;

    elsif p_rule = 'best_avg_recency_weight' then
        with d as (
            select ad.model_group_id, ad.raw_value,
                   (ad.as_of_date - min(ad.as_of_date)
                       over (partition by ad.experiment_hash))::double precision as days_out
            from triage.audition_distances ad
            where ad.experiment_hash = p_experiment and ad.metric = p_metric
              and ad.parameter = p_parameter),
        t as (select max(days_out) as tmax from d),
        wd as (
            select d.model_group_id, d.raw_value,
                   case when t.tmax = 0 then 1.0
                        when p_params->>'decay_type' = 'linear'
                          then ((p_params->>'curr_weight')::double precision - 1.0)
                               * (d.days_out / t.tmax) + 1.0
                        when p_params->>'decay_type' = 'exponential'
                          then exp(ln((p_params->>'curr_weight')::double precision)
                                   * d.days_out / t.tmax)
                        else 1.0 end as weight
            from d cross join t)
        select wd.model_group_id into result from wd
        group by wd.model_group_id
        order by case when hib
                   then sum(wd.raw_value * wd.weight) / nullif(sum(wd.weight), 0)
                 end desc nulls last,
                 case when hib then null
                   else sum(wd.raw_value * wd.weight) / nullif(sum(wd.weight), 0)
                 end asc,
                 wd.model_group_id
        limit 1;

    elsif p_rule = 'best_average_two_metrics' then
        with w as (
            select ad.model_group_id, ad.as_of_date,
                   sum(case
                         when ad.metric = p_metric and ad.parameter = p_parameter
                           then ad.raw_value * (p_params->>'metric1_weight')::double precision
                         when ad.metric = (p_params->>'metric2')
                          and ad.parameter = coalesce(p_params->>'parameter2', '')
                           then ad.raw_value * (1.0 - (p_params->>'metric1_weight')::double precision)
                       end) as weighted
            from triage.audition_distances ad
            where ad.experiment_hash = p_experiment
              and ((ad.metric = p_metric and ad.parameter = p_parameter)
                or (ad.metric = (p_params->>'metric2')
                    and ad.parameter = coalesce(p_params->>'parameter2', '')))
            group by ad.model_group_id, ad.as_of_date)
        select w.model_group_id into result from w
        group by w.model_group_id
        order by case when hib then avg(w.weighted) end desc nulls last,
                 case when hib then null else avg(w.weighted) end asc,
                 w.model_group_id
        limit 1;

    elsif p_rule = 'random_model_group' then
        select a.model_group_id into result from triage.audition a
        where a.experiment_hash = p_experiment and a.metric = p_metric
          and a.parameter = p_parameter
        order by md5(a.model_group_id::text || coalesce(p_params->>'seed', '0')),
                 a.model_group_id
        limit 1;

    else
        raise exception 'unknown audition rule %', p_rule;
    end if;

    return result;
end;
$fn$;

-- latest model for a (experiment, model_group): newest train_end_time across the
-- experiment's runs (cache-shared models keep their original run_id, all under this hash).
drop function if exists triage.latest_model(uuid, bigint);
create or replace function triage.latest_model(p_experiment text, p_group bigint)
returns bigint language sql stable as $$
  select m.model_id
  from   triage.models m join triage.runs r on r.run_id = m.run_id
  where  r.experiment_hash = p_experiment and m.model_group_id = p_group
  order by m.train_end_time desc nulls last, m.model_id desc
  limit 1;
$$;

drop function if exists triage.selected_model(uuid, text, text, text);
create or replace function triage.selected_model(
    p_experiment text, p_metric text, p_parameter text default '',
    p_rule text default 'best_average_value')
returns table(audition_group bigint, audition_model bigint,
              leaderboard_group bigint, leaderboard_model bigint, diverges boolean)
language sql stable as $$
  select s.ag, triage.latest_model(p_experiment, s.ag),
         s.lg, triage.latest_model(p_experiment, s.lg),
         (s.ag is distinct from s.lg)
  from (select triage.audition_pick(p_experiment, p_metric, p_parameter, p_rule, '{}'::jsonb) as ag,
               triage.audition_pick(p_experiment, p_metric, p_parameter,
                                    'best_current_value', '{}'::jsonb)                       as lg) s;
$$;

-- leaderboard matview: add experiment_hash (still carries run_id). Refreshed at run end.
drop materialized view if exists triage.leaderboard;
create materialized view triage.leaderboard as
select r.experiment_hash, m.run_id, mg.model_group_id, mg.model_type, e.split_kind,
       e.metric, e.parameter, e.as_of_date,
       e.value, e.value_expected, e.value_std, m.model_id, m.train_end_time
from   triage.evaluations e
join   triage.models       m  on m.model_id = e.model_id
join   triage.model_groups mg on mg.model_group_id = m.model_group_id
join   triage.runs         r  on r.run_id = m.run_id
with no data;

-- --------------------------------------------------------------- hierarchy reads
-- per (experiment, model_group): the static card facts; metric-specific "best" is joined
-- from audition/leaderboard by the endpoint (kept metric-agnostic here).
create or replace view triage.model_group_summary as
select r.experiment_hash, mg.model_group_id, mg.model_group_hash, mg.model_type,
       mg.hyperparameters, mg.feature_list,
       count(distinct m.model_id) as n_models,
       min(m.train_end_time)      as first_train_end,
       max(m.train_end_time)      as last_train_end
from   triage.model_groups mg
join   triage.models m on m.model_group_id = mg.model_group_id
join   triage.runs   r on r.run_id = m.run_id
group by r.experiment_hash, mg.model_group_id, mg.model_group_hash, mg.model_type,
         mg.hyperparameters, mg.feature_list;

-- distinct (metric, parameter) actually present, with direction — for SPA selectors.
create or replace view triage.metric_catalog as
select distinct e.metric, e.parameter, triage.higher_is_better(e.metric) as higher_is_better
from   triage.evaluations e where e.split_kind = 'test';

-- project-wide derivation sharing: how many experiments/runs touch each artifact (the
-- "shared node" marking for the cross-experiment graph, Q6-b).
create or replace view triage.artifact_sharing as
select ra.artifact_id,
       count(distinct r.experiment_hash) as n_experiments,
       count(distinct ra.run_id)         as n_runs
from   triage.run_artifacts ra
join   triage.runs r on r.run_id = ra.run_id
group by ra.artifact_id;

-- --------------------------------------------------------------- model detail
-- The classic Rayid curve: at each population cut k (= rank_abs), precision/recall and the
-- full confusion (tp/fp/fn/tn) over this model's labeled test predictions. The client
-- k-slider reads/interpolates this series (no UI business logic — ADR-0012). Labels join via
-- the model's run's single labels artifact (greenfield: one labels per run) on
-- (entity_id, as_of_date) at the model's training_label_timespan.
create or replace function triage.model_threshold_curve(p_model bigint)
returns table(k integer, pct double precision, prec double precision, rec double precision,
              tp bigint, fp bigint, fn bigint, tn bigint)
language sql stable as $$
  with ranked as (
    select pr.rank_abs, l.outcome
    from   triage.prediction_ranks pr
    join   triage.models m on m.model_id = pr.model_id
    join   triage.run_artifacts ra on ra.run_id = m.run_id
    join   triage.artifacts a on a.artifact_id = ra.artifact_id and a.kind = 'labels'
    join   triage.labels l on l.label_hash = ra.artifact_id and l.entity_id = pr.entity_id
                          and l.as_of_date = pr.as_of_date
                          and l.label_timespan = m.training_label_timespan
    where  pr.model_id = p_model and pr.split_kind = 'test' and l.outcome is not null),
  tot as (select count(*) as n, coalesce(sum(outcome), 0) as p from ranked),
  cum as (
    select rank_abs as flagged,
           sum(outcome) over (order by rank_abs) as tp
    from   ranked)
  select cum.flagged::int                                   as k,
         cum.flagged::double precision / nullif(tot.n, 0)   as pct,
         cum.tp / cum.flagged                               as prec,
         cum.tp / nullif(tot.p, 0)                          as rec,
         cum.tp::bigint                                     as tp,
         (cum.flagged - cum.tp)::bigint                     as fp,
         (tot.p - cum.tp)::bigint                           as fn,
         (tot.n - tot.p - (cum.flagged - cum.tp))::bigint   as tn
  from   cum cross join tot
  order by cum.flagged;
$$;

-- predicted-score histogram (by class) for the model card.
create or replace function triage.model_score_histogram(p_model bigint, p_bins integer default 20)
returns table(bin integer, lo double precision, hi double precision, n bigint, n_pos bigint)
language sql stable as $$
  with s as (
    select pr.score, l.outcome
    from   triage.prediction_ranks pr
    join   triage.models m on m.model_id = pr.model_id
    join   triage.run_artifacts ra on ra.run_id = m.run_id
    join   triage.artifacts a on a.artifact_id = ra.artifact_id and a.kind = 'labels'
    left join triage.labels l on l.label_hash = ra.artifact_id and l.entity_id = pr.entity_id
                          and l.as_of_date = pr.as_of_date
                          and l.label_timespan = m.training_label_timespan
    where  pr.model_id = p_model and pr.split_kind = 'test' and pr.score is not null),
  b as (select width_bucket(score, 0.0, 1.0, p_bins) as bin, outcome from s)
  select b.bin,
         (b.bin - 1)::double precision / p_bins as lo,
         (b.bin)::double precision     / p_bins as hi,
         count(*)                               as n,
         count(*) filter (where b.outcome = 1)  as n_pos
  from   b group by b.bin order by b.bin;
$$;

-- --------------------------------------------------------------- ontology source profile
-- Volume of a registered source relation over time, via its knowledge_date_column. Generic
-- (the project-specific clean/ontology table name lives in triage.sources) and injection-safe
-- (regclass validates the relation; %I quotes the date column).
create or replace function triage.source_volume(p_source text, p_grain text default 'month')
returns table(period date, n bigint)
language plpgsql stable as $fn$
declare rel text; kd text;
begin
    select s.relation, s.knowledge_date_column into rel, kd
    from triage.sources s where s.source_name = p_source;
    if rel is null then return; end if;
    if kd is null then
        return query execute format(
            'select null::date as period, count(*)::bigint as n from %s', rel::regclass::text);
    else
        return query execute format(
            'select date_trunc(%L, %I)::date as period, count(*)::bigint as n'
            || ' from %s group by 1 order by 1', p_grain, kd, rel::regclass::text);
    end if;
end;
$fn$;
"""


DOWNGRADE_DDL = r"""
drop function if exists triage.source_volume(text, text);
drop function if exists triage.model_score_histogram(bigint, integer);
drop function if exists triage.model_threshold_curve(bigint);
drop view if exists triage.artifact_sharing;
drop view if exists triage.metric_catalog;
drop view if exists triage.model_group_summary;
drop view if exists triage.experiment_summary;

-- restore the 0004 run-scoped audition layer
drop function if exists triage.selected_model(text, text, text, text);
drop function if exists triage.latest_model(text, bigint);
drop function if exists triage.audition_pick(text, text, text, text, jsonb);
drop view if exists triage.audition;
drop view if exists triage.audition_distances;

create view triage.audition_distances as
select m.run_id, m.model_group_id, e.metric, e.parameter, e.as_of_date,
       m.train_end_time, e.value as raw_value,
       case when triage.higher_is_better(e.metric)
            then max(e.value) over w else min(e.value) over w end as best_value,
       abs((case when triage.higher_is_better(e.metric)
                 then max(e.value) over w else min(e.value) over w end) - e.value)
         as dist_from_best_case
from   triage.evaluations e
join   triage.models      m using (model_id)
where  e.split_kind = 'test' and e.value is not null
window w as (partition by m.run_id, e.metric, e.parameter, e.as_of_date);

create view triage.audition as
select run_id, metric, parameter, model_group_id,
       count(*) as n_splits_evaluated, avg(raw_value) as avg_value,
       stddev_samp(raw_value) as stddev_value, avg(dist_from_best_case) as avg_distance_from_best,
       max(dist_from_best_case) as max_regret
from triage.audition_distances
group by run_id, metric, parameter, model_group_id;

-- (the 0004 run-scoped audition_pick/latest_model/selected_model bodies are restored by
-- downgrading to 0004; this downgrade only needs to remove 0005's experiment-scoped layer
-- and the experiment-scoped distances/audition views so 0004's upgrade can recreate the rest.)

drop materialized view if exists triage.leaderboard;
create materialized view triage.leaderboard as
select m.run_id, mg.model_group_id, mg.model_type, e.split_kind,
       e.metric, e.parameter, e.as_of_date,
       e.value, e.value_expected, e.value_std, m.model_id, m.train_end_time
from   triage.evaluations e
join   triage.models       m  using (model_id)
join   triage.model_groups mg using (model_group_id)
with no data;

alter table triage.experiments drop column if exists author;
alter table triage.experiments drop column if exists description;
alter table triage.experiments drop column if exists name;
drop function if exists triage.auto_experiment_name(text);
"""


def upgrade():
    op.execute(UPGRADE_DDL)


def downgrade():
    op.execute(DOWNGRADE_DDL)
