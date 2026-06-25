"""rayid curve: rank within the labeled population (fix pct > 100%)

Revision ID: 0007_rayid_curve_labeled_population
Revises: 0006_dashboard_drilldowns
Create Date: 2026-06-25

``model_threshold_curve`` used ``prediction_ranks.rank_abs`` — a rank over the FULL scored
population (cohort), including entities with no matured label — as both the population cut
(``flagged``) and the precision denominator, while the total (``tot.n``) counted only the
LABELED predictions. So ``pct = rank_abs / n_labeled`` ran past 1.0 (the live food model 183
peaked at 228%) and ``prec = tp / rank_abs`` was divided by the wrong denominator, so the
slider's "precision @ 10%" disagreed with the leaderboard's precision@10%.

Fix: re-rank within the labeled subset (``row_number() over (order by rank_abs)`` preserves the
score order), so ``flagged`` and the precision denominator are both the labeled count. The
curve is now a clean 0–100% over the evaluable population and its precision@k% matches the
in-PG metric functions / leaderboard. The labels join is unchanged (one labels artifact per
run, greenfield).
"""

from alembic import op

# revision identifiers, used by Alembic. (id <= 32 chars: results_schema_versions.version_num)
revision = "0007_rayid_curve_labeled"
down_revision = "0006_dashboard_drilldowns"
branch_labels = None
depends_on = None


CURVE_LABELED = r"""
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
  ordered as (
    -- re-rank within the LABELED subset: rank_abs spans the full scored population (incl.
    -- unlabeled), so using it as the cut overshoots the labeled total (pct > 100%) and
    -- mis-scales precision. row_number preserves the score order over labeled rows only.
    select outcome, row_number() over (order by rank_abs) as flagged
    from   ranked),
  tot as (select count(*) as n, coalesce(sum(outcome), 0) as p from ranked),
  cum as (select flagged, sum(outcome) over (order by flagged) as tp from ordered)
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
"""


# The 0005/0006 version: rank_abs as the population cut (the buggy one), restored on downgrade.
CURVE_RANK_ABS = r"""
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
"""


def upgrade():
    op.execute(CURVE_LABELED)


def downgrade():
    op.execute(CURVE_RANK_ABS)
