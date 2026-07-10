"""dogfooding fixes — experiment-scoped label history + monitoring subset guard (plan P12.4)

Revision ID: 0018_label_history_monitoring
Revises: 0017_diagnostics_tables
Create Date: 2026-07-10

Two defects surfaced by the 2026-07-09 dashboard dogfooding session on chi311:

* ``entity_label_history`` (0008) crossed the entity's cohort dates with *all* label
  timespans project-wide, so the entity drawer opened from the classification
  experiment also showed the survival experiment's rows — whose ``outcome`` is NULL
  by design (ADR-0010: survival fills ``duration``/``event_observed``), rendered as a
  misleading "no matured label". The function now takes an optional experiment hash
  (grid/spans/pick scoped to that experiment's cohort + label artifacts via the
  ``run_artifacts`` lineage) and returns ``duration``/``event_observed`` alongside
  ``outcome`` so survival rows are legible.

* ``monitoring_outcome_tracking`` (0012) predates subset evaluations (0015) and
  carried no ``subset_hash`` — subset-filtered AUCs plotted on the full-cohort line
  (the P3 "subset guards on all evaluation readers" sweep missed this view). The
  view now exposes ``subset_hash`` (appended last — CREATE OR REPLACE compatible)
  and the dashboard route guards ``subset_hash = ''``.
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "0018_label_history_monitoring"
down_revision = "0017_diagnostics_tables"
branch_labels = None
depends_on = None


UPGRADE_DDL = r"""
-- ------------------------------------------------ entity label history, experiment-scoped
-- Return type changes (adds duration/event_observed) so CREATE OR REPLACE cannot apply.
drop function triage.entity_label_history(bigint);

create function triage.entity_label_history(p_entity bigint, p_experiment text default null)
returns table(as_of_date date, label_timespan interval, outcome double precision,
              duration double precision, event_observed boolean)
language sql stable as $$
    with exp_artifacts as (
        -- cohort + label artifacts reachable from the experiment's runs; empty when unscoped
        select ra.artifact_id, a.kind
        from   triage.run_artifacts ra
        join   triage.runs r using (run_id)
        join   triage.artifacts a on a.artifact_id = ra.artifact_id
        where  p_experiment is not null
          and  r.experiment_hash = p_experiment
          and  a.kind in ('cohort', 'labels')
    ),
    grid as (
        select distinct c.as_of_date
        from   triage.cohorts c
        where  c.entity_id = p_entity
          and  (p_experiment is null
                or c.cohort_hash in (select artifact_id from exp_artifacts where kind = 'cohort'))
    ),
    spans as (
        select distinct l.label_timespan
        from   triage.labels l
        where  (p_experiment is null
                or l.label_hash in (select artifact_id from exp_artifacts where kind = 'labels'))
    )
    select g.as_of_date, sp.label_timespan, pick.outcome, pick.duration, pick.event_observed
    from   grid g
    cross  join spans sp
    left   join lateral (
        select l.outcome, l.duration, l.event_observed
        from   triage.labels l
        where  l.entity_id = p_entity
          and  l.as_of_date = g.as_of_date
          and  l.label_timespan = sp.label_timespan
          and  (p_experiment is null
                or l.label_hash in (select artifact_id from exp_artifacts where kind = 'labels'))
        order by l.created_at desc
        limit 1
    ) pick on true
    order by g.as_of_date, sp.label_timespan;
$$;

-- ------------------------------------------------ monitoring outcomes gain subset_hash
-- Appended as the trailing column so CREATE OR REPLACE VIEW applies in place.
create or replace view triage.monitoring_outcome_tracking as
select mg.model_group_id,
       m.model_id,
       r.purpose,
       e.split_kind,
       e.as_of_date,
       e.metric,
       e.parameter,
       e.value,
       e.num_labeled,
       e.computed_at,
       e.subset_hash
from   triage.evaluations  e
join   triage.models       m  using (model_id)
join   triage.model_groups mg using (model_group_id)
left   join triage.runs    r  on r.run_id = m.run_id;
"""


# Restore the 0008 function and 0012 view bodies verbatim on downgrade
# (the view must be dropped: CREATE OR REPLACE cannot remove a column).
DOWNGRADE_DDL = r"""
drop function triage.entity_label_history(bigint, text);

create function triage.entity_label_history(p_entity bigint)
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

drop view triage.monitoring_outcome_tracking;

create view triage.monitoring_outcome_tracking as
select mg.model_group_id,
       m.model_id,
       r.purpose,
       e.split_kind,
       e.as_of_date,
       e.metric,
       e.parameter,
       e.value,
       e.num_labeled,
       e.computed_at
from   triage.evaluations  e
join   triage.models       m  using (model_id)
join   triage.model_groups mg using (model_group_id)
left   join triage.runs    r  on r.run_id = m.run_id;
"""


def upgrade():
    op.execute(UPGRADE_DDL)


def downgrade():
    op.execute(DOWNGRADE_DDL)
