"""dashboard read views + in-PG audition (read-dashboard-spec §3)

Revision ID: 0004_dashboard_reads
Revises: 0003_run_provenance
Create Date: 2026-06-21

The read-dashboard's logic-free contract (ADR-0012): every panel maps to a view/
function. This migration adds that SQL layer (docs/read-dashboard-spec.md §3),
all thin SQL over existing tables — no new business logic enters the system.

Grounding (0001 baseline): nothing is naively run_id-keyed. evaluations are
model_id-keyed (scope via triage.models.run_id); cohorts/labels are
*_hash/artifact-keyed (scope via run_artifacts + artifacts.kind); the leaderboard
matview carried no run_id (added here).

Adds:
  * runs.plan (jsonb)              — DAG denominators + summary (ADR-0021 telemetry;
                                     populated by run_experiment, app-side).
  * higher_is_better(metric)       — centralized metric direction (ported from
                                     audition/metric_directionality.py).
  * leaderboard (matview)          — recreated with run_id + split_kind.
  * run_summary / cohort_profile / label_base_rate / run_progress  — read views.
  * audition_distances / audition  — per-split distance-from-best + per-group
                                     aggregates (ports distance_from_best/regrets).
  * audition_pick(run,metric,parameter,rule,params) — the full standard selection-
                                     rule catalog (ported from selection_rules.py).
  * latest_model / selected_model  — the §2-C selector defaults (audition pick vs
                                     leaderboard #1 + divergence), logic-free.

Time axis: the rules in selection_rules.py key on train_end_time; in greenfield
each model_group has one model per split, so train_end_time and the test
as_of_date are 1:1 and we use as_of_date as the single comparison/recency axis.
Tie-break is deterministic on model_group_id (selection_rules.py shuffles ties;
we prefer reproducibility). Validate each rule vs src/tests/audition_tests/.

Raw SQL in op.execute, mirroring 0001-0003; $$ / $fn$ delimit function bodies.
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "0004_dashboard_reads"
down_revision = "0003_run_provenance"
branch_labels = None
depends_on = None


UPGRADE_DDL = r"""
-- ---------------------------------------------------------------- runs.plan
alter table triage.runs add column if not exists plan jsonb;

-- ----------------------------------------------- metric direction (centralized)
-- Ported from audition/metric_directionality.py: lower-is-better is the small
-- closed set; everything else (precision@/recall@/auc_roc/average_precision/r2,
-- and unknown metrics) is higher-is-better.
create or replace function triage.higher_is_better(metric text)
returns boolean language sql immutable as $$
  select metric is null
      or metric not in ('rmse', 'mae',
                        'false positives@', 'false negatives@', 'fpr@');
$$;

-- ----------------------------------------- leaderboard matview (+ run_id, split)
drop materialized view if exists triage.leaderboard;
create materialized view triage.leaderboard as
select m.run_id, mg.model_group_id, mg.model_type, e.split_kind,
       e.metric, e.parameter, e.as_of_date,
       e.value, e.value_expected, e.value_std,
       m.model_id, m.train_end_time
from   triage.evaluations e
join   triage.models       m  using (model_id)
join   triage.model_groups mg using (model_group_id)
with no data;

-- --------------------------------------------------------------- read views
create or replace view triage.run_summary as
select r.run_id, r.status, r.profile, r.purpose, r.started_at, r.finished_at,
       (r.finished_at - r.started_at) as duration,
       r.random_seed, r.triage_version, r.git_hash, r.batch_job_id,
       e.experiment_hash, e.problem_type,
       e.config as experiment_config,        -- cohort/label names live here
       r.plan                                 -- splits, #features, grid, engine versions
from   triage.runs r
left join triage.experiments e using (experiment_hash);

-- cohorts/labels are artifact-keyed; scope to a run via run_artifacts + kind.
create or replace view triage.cohort_profile as
select ra.run_id, c.as_of_date, count(distinct c.entity_id) as n_entities
from   triage.run_artifacts ra
join   triage.artifacts a on a.artifact_id = ra.artifact_id and a.kind = 'cohort'
join   triage.cohorts   c on c.cohort_hash = ra.artifact_id
group by ra.run_id, c.as_of_date;

create or replace view triage.label_base_rate as
select ra.run_id, l.as_of_date, l.label_timespan,
       avg(l.outcome) filter (where l.outcome is not null) as base_rate,
       count(*)       filter (where l.outcome is not null) as n_labeled
from   triage.run_artifacts ra
join   triage.artifacts a on a.artifact_id = ra.artifact_id and a.kind = 'labels'
join   triage.labels    l on l.label_hash  = ra.artifact_id
group by ra.run_id, l.as_of_date, l.label_timespan;

-- in-flight pipeline DAG state (built_by_run surfaces 'building' nodes that the
-- post-build run_artifacts usage edges would miss). Denominators from runs.plan.
create or replace view triage.run_progress as
select a.built_by_run as run_id, a.kind, a.status, count(*) as n
from   triage.artifacts a
where  a.built_by_run is not null
group by a.built_by_run, a.kind, a.status;

-- ------------------------------------------------------------------ audition
-- Per-split distance-from-best (the granular building block the rules need):
-- best at each (run, metric, parameter, as_of_date) is direction-aware across
-- model_groups; dist_from_best_case = |best - value|.
create or replace view triage.audition_distances as
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

-- Per-model_group aggregates (the comparison surface: avg/regret/variance).
create or replace view triage.audition as
select run_id, metric, parameter, model_group_id,
       count(*)                  as n_splits_evaluated,
       avg(raw_value)            as avg_value,
       stddev_samp(raw_value)    as stddev_value,
       avg(dist_from_best_case)  as avg_distance_from_best,
       max(dist_from_best_case)  as max_regret
from   triage.audition_distances
group by run_id, metric, parameter, model_group_id;

-- The standard selection-rule catalog (ported from selection_rules.py). Returns
-- the chosen model_group_id; deterministic tie-break on model_group_id.
create or replace function triage.audition_pick(
    p_run uuid, p_metric text, p_parameter text default '',
    p_rule text default 'best_current_value', p_params jsonb default '{}'::jsonb)
returns bigint language plpgsql stable as $fn$
declare
    hib boolean := triage.higher_is_better(p_metric);
    result bigint;
begin
    if p_rule = 'best_current_value' then
        select ad.model_group_id into result
        from triage.audition_distances ad
        where ad.run_id = p_run and ad.metric = p_metric and ad.parameter = p_parameter
          and ad.as_of_date = (select max(as_of_date) from triage.audition_distances
                               where run_id = p_run and metric = p_metric
                                 and parameter = p_parameter)
        order by case when hib then ad.raw_value end desc nulls last,
                 case when hib then null else ad.raw_value end asc,
                 ad.model_group_id
        limit 1;

    elsif p_rule = 'best_average_value' then
        select a.model_group_id into result from triage.audition a
        where a.run_id = p_run and a.metric = p_metric and a.parameter = p_parameter
        order by case when hib then a.avg_value end desc nulls last,
                 case when hib then null else a.avg_value end asc,
                 a.model_group_id
        limit 1;

    elsif p_rule = 'lowest_metric_variance' then
        select a.model_group_id into result from triage.audition a
        where a.run_id = p_run and a.metric = p_metric and a.parameter = p_parameter
        order by a.stddev_value asc nulls last, a.model_group_id
        limit 1;

    elsif p_rule = 'most_frequent_best_dist' then
        select ad.model_group_id into result
        from triage.audition_distances ad
        where ad.run_id = p_run and ad.metric = p_metric and ad.parameter = p_parameter
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
            where ad.run_id = p_run and ad.metric = p_metric and ad.parameter = p_parameter
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
                   (ad.as_of_date - min(ad.as_of_date) over (partition by ad.run_id))::double precision
                     as days_out
            from triage.audition_distances ad
            where ad.run_id = p_run and ad.metric = p_metric and ad.parameter = p_parameter),
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
            where ad.run_id = p_run
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
        where a.run_id = p_run and a.metric = p_metric and a.parameter = p_parameter
        order by md5(a.model_group_id::text || coalesce(p_params->>'seed', '0')),
                 a.model_group_id
        limit 1;

    else
        raise exception 'unknown audition rule %', p_rule;
    end if;

    return result;
end;
$fn$;

-- ------------------------------------------------ selected-model (the §2-C bar)
-- The concrete model for a (run, model_group) is its latest-split model.
create or replace function triage.latest_model(p_run uuid, p_group bigint)
returns bigint language sql stable as $$
  select model_id from triage.models
  where run_id = p_run and model_group_id = p_group
  order by train_end_time desc nulls last, model_id desc
  limit 1;
$$;

-- Defaults the bar reads logic-free: the audition pick (p_rule) vs leaderboard #1
-- (= best_current_value), each resolved to a model_id, plus the divergence flag.
create or replace function triage.selected_model(
    p_run uuid, p_metric text, p_parameter text default '',
    p_rule text default 'best_average_value')
returns table(audition_group bigint, audition_model bigint,
              leaderboard_group bigint, leaderboard_model bigint, diverges boolean)
language sql stable as $$
  select s.ag, triage.latest_model(p_run, s.ag),
         s.lg, triage.latest_model(p_run, s.lg),
         (s.ag is distinct from s.lg)
  from (select triage.audition_pick(p_run, p_metric, p_parameter, p_rule, '{}'::jsonb) as ag,
               triage.audition_pick(p_run, p_metric, p_parameter,
                                    'best_current_value', '{}'::jsonb)              as lg) s;
$$;
"""


DOWNGRADE_DDL = r"""
drop function if exists triage.selected_model(uuid, text, text, text);
drop function if exists triage.latest_model(uuid, bigint);
drop function if exists triage.audition_pick(uuid, text, text, text, jsonb);
drop view if exists triage.audition;
drop view if exists triage.audition_distances;
drop view if exists triage.run_progress;
drop view if exists triage.label_base_rate;
drop view if exists triage.cohort_profile;
drop view if exists triage.run_summary;

-- restore the original 0001 leaderboard (no run_id / split_kind)
drop materialized view if exists triage.leaderboard;
create materialized view triage.leaderboard as
select mg.model_group_id, mg.model_type,
       e.metric, e.parameter, e.as_of_date,
       e.value, e.value_expected, e.value_std,
       m.model_id, m.train_end_time
from   triage.evaluations e
join   triage.models       m  using (model_id)
join   triage.model_groups mg using (model_group_id)
with no data;

drop function if exists triage.higher_is_better(text);
alter table triage.runs drop column if exists plan;
"""


def upgrade():
    op.execute(UPGRADE_DDL)


def downgrade():
    op.execute(DOWNGRADE_DDL)
