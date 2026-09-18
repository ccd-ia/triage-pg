"""audition date window — select on a regime, not on all of history (#13)

Revision ID: 0022_audition_window
Revises: 0021_model_feature_geometry
Create Date: 2026-09-17

``triage.audition`` aggregates ``audition_distances`` over **every** evaluated split of an
experiment, and ``audition_pick`` / ``selected_model`` took no dates. A selection window — "the
last two seasons", because that is the regime the served model has to survive — could not be
expressed. ``best_avg_recency_weight`` down-weights old splits but still includes them, and its
weights are relative to the experiment's own first date.

This adds ``p_from date default null, p_to date default null`` to both functions and a
``triage.audition_windowed`` function with the same columns as ``triage.audition``. Both bounds
null reproduces the previous behaviour exactly, so every existing five-argument call keeps
resolving through the defaults and returning what it returned before.

**The window is applied before every rule's aggregation, not after.** That matters most for
``best_avg_recency_weight``: its ``days_out`` is measured from the earliest date *in scope*, so
with a window the decay is relative to the window's start rather than the experiment's. That is
the intended reading of "weight recent splits inside this regime", and it is a genuine behaviour
change for anyone who passes bounds — stated here rather than discovered.

The three rules that read the aggregate ``triage.audition`` view (``best_average_value``,
``lowest_metric_variance``, ``random_model_group``) read ``audition_windowed`` instead, since the
view itself has no date column to filter on.
"""

from alembic import op

revision = "0022_audition_window"
down_revision = "0021_model_feature_geometry"
branch_labels = None
depends_on = None


# The windowed twin of triage.audition. Same columns, same semantics, plus two bounds.
WINDOWED = """
drop function if exists triage.audition_windowed(text, text, text, date, date);
create or replace function triage.audition_windowed(
    p_experiment text, p_metric text, p_parameter text default '',
    p_from date default null, p_to date default null)
returns table(experiment_hash text, metric text, parameter text, model_group_id bigint,
              n_splits_evaluated bigint, avg_value double precision,
              stddev_value double precision, avg_distance_from_best double precision,
              max_regret double precision, avg_regret_next_time double precision,
              max_regret_next_time double precision)
language sql stable as $$
  select ad.experiment_hash, ad.metric, ad.parameter, ad.model_group_id,
         count(*)                            as n_splits_evaluated,
         avg(ad.raw_value)                   as avg_value,
         stddev_samp(ad.raw_value)           as stddev_value,
         avg(ad.dist_from_best_case)         as avg_distance_from_best,
         max(ad.dist_from_best_case)         as max_regret,
         avg(ad.dist_from_best_case_next_time) as avg_regret_next_time,
         max(ad.dist_from_best_case_next_time) as max_regret_next_time
  from   triage.audition_distances ad
  where  ad.experiment_hash = p_experiment and ad.metric = p_metric
    and  ad.parameter = p_parameter
    and  ad.as_of_date between coalesce(p_from, '-infinity'::date)
                           and coalesce(p_to,   'infinity'::date)
  group by ad.experiment_hash, ad.metric, ad.parameter, ad.model_group_id;
$$;
"""

# audition_pick, re-created with the two bounds threaded into every rule. The body is the 0005
# body plus the date predicate; the aggregate-view rules read audition_windowed instead.
PICK = """
drop function if exists triage.audition_pick(text, text, text, text, jsonb);
drop function if exists triage.audition_pick(text, text, text, text, jsonb, date, date);
create or replace function triage.audition_pick(
    p_experiment text, p_metric text, p_parameter text default '',
    p_rule text default 'best_current_value', p_params jsonb default '{}'::jsonb,
    p_from date default null, p_to date default null)
returns bigint language plpgsql stable as $fn$
declare
    hib boolean := triage.higher_is_better(p_metric);
    lo  date := coalesce(p_from, '-infinity'::date);
    hi  date := coalesce(p_to,   'infinity'::date);
    result bigint;
begin
    if p_rule = 'best_current_value' then
        select ad.model_group_id into result
        from triage.audition_distances ad
        where ad.experiment_hash = p_experiment and ad.metric = p_metric
          and ad.parameter = p_parameter
          and ad.as_of_date between lo and hi
          and ad.as_of_date = (select max(as_of_date) from triage.audition_distances
                               where experiment_hash = p_experiment and metric = p_metric
                                 and parameter = p_parameter
                                 and as_of_date between lo and hi)
        order by case when hib then ad.raw_value end desc nulls last,
                 case when hib then null else ad.raw_value end asc,
                 ad.model_group_id
        limit 1;

    elsif p_rule = 'best_average_value' then
        select a.model_group_id into result
        from triage.audition_windowed(p_experiment, p_metric, p_parameter, p_from, p_to) a
        order by case when hib then a.avg_value end desc nulls last,
                 case when hib then null else a.avg_value end asc,
                 a.model_group_id
        limit 1;

    elsif p_rule = 'lowest_metric_variance' then
        select a.model_group_id into result
        from triage.audition_windowed(p_experiment, p_metric, p_parameter, p_from, p_to) a
        order by a.stddev_value asc nulls last, a.model_group_id
        limit 1;

    elsif p_rule = 'most_frequent_best_dist' then
        select ad.model_group_id into result
        from triage.audition_distances ad
        where ad.experiment_hash = p_experiment and ad.metric = p_metric
          and ad.parameter = p_parameter
          and ad.as_of_date between lo and hi
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
              and ad.as_of_date between lo and hi
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
        -- days_out is measured from the earliest date IN SCOPE, so with a window the decay is
        -- relative to the window's start rather than the experiment's (see the module docstring).
        with d as (
            select ad.model_group_id, ad.raw_value,
                   (ad.as_of_date - min(ad.as_of_date)
                       over (partition by ad.experiment_hash))::double precision as days_out
            from triage.audition_distances ad
            where ad.experiment_hash = p_experiment and ad.metric = p_metric
              and ad.parameter = p_parameter
              and ad.as_of_date between lo and hi),
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
              and ad.as_of_date between lo and hi
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
        select a.model_group_id into result
        from triage.audition_windowed(p_experiment, p_metric, p_parameter, p_from, p_to) a
        order by md5(a.model_group_id::text || coalesce(p_params->>'seed', '0')),
                 a.model_group_id
        limit 1;

    else
        raise exception 'unknown audition rule %', p_rule;
    end if;

    return result;
end;
$fn$;
"""

SELECTED = """
drop function if exists triage.selected_model(text, text, text, text);
drop function if exists triage.selected_model(text, text, text, text, date, date);
create or replace function triage.selected_model(
    p_experiment text, p_metric text, p_parameter text default '',
    p_rule text default 'best_average_value',
    p_from date default null, p_to date default null)
returns table(audition_group bigint, audition_model bigint,
              leaderboard_group bigint, leaderboard_model bigint, diverges boolean)
language sql stable as $$
  select s.ag, triage.latest_model(p_experiment, s.ag),
         s.lg, triage.latest_model(p_experiment, s.lg),
         (s.ag is distinct from s.lg)
  from (select triage.audition_pick(p_experiment, p_metric, p_parameter, p_rule,
                                    '{}'::jsonb, p_from, p_to) as ag,
               triage.audition_pick(p_experiment, p_metric, p_parameter,
                                    'best_current_value', '{}'::jsonb, p_from, p_to) as lg) s;
$$;
"""

# The 0005 definitions, verbatim — what a downgrade must restore. Extracted from that
# migration's own text rather than retyped, so the two cannot drift.
PRE_WINDOW_DDL = r"""
drop function if exists triage.audition_pick(text, text, text, text, jsonb, date, date);
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

drop function if exists triage.selected_model(text, text, text, text, date, date);
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
"""


def upgrade() -> None:
    op.execute(WINDOWED)
    op.execute(PICK)
    op.execute(SELECTED)


def downgrade() -> None:
    op.execute(
        "drop function if exists triage.audition_windowed(text, text, text, date, date)"
    )
    op.execute(PRE_WINDOW_DDL)
