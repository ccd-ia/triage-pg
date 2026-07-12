"""task framing — the observation regime, orthogonal to problem_type (plan P12.4)

Revision ID: 0019_task_framing
Revises: 0018_label_history_monitoring
Create Date: 2026-07-12

Maintainer-approved close of the 2026-07-09 "%labeled = 100% on an inspections
problem?" dogfooding question. ``problem_type`` drives the scoring machinery
(estimators, label columns, metrics); it says nothing about WHO gets a label.
``task_framing`` names that observation regime:

* ``early_warning``            — the outcome is observed for every cohort member
                                 (%labeled should approach 100).
* ``resource_prioritization``  — inspections-style: outcomes exist only for
                                 acted-on entities (%labeled < 100 is expected,
                                 selective-labels caveats apply).
* ``visit_level``              — the label attaches to an event/visit, not the
                                 entity's period.

Identity-neutral by construction: the ADR-0022 experiment hash covers only the
problem keys (cohort/label/temporal/problem_type), so tagging an existing config
does not fork its history. The column updates on re-run when the config provides
a value and is never cleared by a config that omits it (coalesce upsert in
``adapters/run.py``).

Bundled fix: ``label_base_rate`` counted only ``outcome is not null``, so
survival labels (outcome NULL, ``duration``/``event_observed`` set — ADR-0010)
read as unlabeled and the survival experiment header showed LABELS 0 /
%LABELED 0.0%. The view now counts a row labeled when either column family is
present, and ``base_rate`` falls back to the observed-event rate for survival.
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "0019_task_framing"
down_revision = "0018_label_history_monitoring"
branch_labels = None
depends_on = None


UPGRADE_DDL = r"""
-- ------------------------------------------------ the observation-regime tag
alter table triage.experiments
    add column task_framing text
    check (task_framing in ('early_warning', 'resource_prioritization', 'visit_level'));

-- ------------------------------------------------ surface it (trailing column: CREATE OR
-- REPLACE compatible; leading columns byte-identical to 0008)
create or replace view triage.experiment_summary as
with base as (
    select e.experiment_hash,
           coalesce(nullif(e.name, ''), triage.auto_experiment_name(e.experiment_hash)) as name,
           e.description, e.author, e.problem_type, e.created_at,
           count(r.run_id)                                          as n_runs,
           max(r.started_at)                                        as last_started_at,
           (array_agg(r.status order by r.started_at desc nulls last))[1] as last_status,
           (array_agg(r.plan   order by r.started_at desc nulls last))[1] as last_plan,
           e.archived_at,
           e.task_framing
    from   triage.experiments e
    left join triage.runs r using (experiment_hash)
    group by e.experiment_hash, e.name, e.description, e.author, e.problem_type, e.created_at,
             e.archived_at, e.task_framing
)
select base.experiment_hash, base.name, base.description, base.author, base.problem_type,
       base.created_at, base.n_runs, base.last_started_at, base.last_status, base.last_plan,
       a.n_model_groups, a.n_models, a.n_splits, a.n_features, a.base_rate, a.cohort_size,
       base.archived_at,
       base.task_framing
from   base
left join triage.experiment_actuals a using (experiment_hash);

-- ------------------------------------------------ survival-aware label counting
-- A survival label (ADR-0010) fills duration/event_observed and leaves outcome NULL;
-- base_rate for survival = the observed-event rate (the closest analogue of a base rate).
create or replace view triage.label_base_rate as
select ra.run_id, l.as_of_date, l.label_timespan,
       coalesce(
           avg(l.outcome)             filter (where l.outcome is not null),
           avg(l.event_observed::int) filter (where l.event_observed is not null)
       ) as base_rate,
       count(*) filter (where l.outcome is not null or l.duration is not null) as n_labeled
from   triage.run_artifacts ra
join   triage.artifacts a on a.artifact_id = ra.artifact_id and a.kind = 'labels'
join   triage.labels    l on l.label_hash  = ra.artifact_id
group by ra.run_id, l.as_of_date, l.label_timespan;
"""


# Restore the 0008 experiment_summary and 0004 label_base_rate bodies verbatim; the view
# must be dropped first (CREATE OR REPLACE cannot remove a column), then the column goes.
DOWNGRADE_DDL = r"""
drop view triage.experiment_summary;

create view triage.experiment_summary as
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

create or replace view triage.label_base_rate as
select ra.run_id, l.as_of_date, l.label_timespan,
       avg(l.outcome) filter (where l.outcome is not null) as base_rate,
       count(*)       filter (where l.outcome is not null) as n_labeled
from   triage.run_artifacts ra
join   triage.artifacts a on a.artifact_id = ra.artifact_id and a.kind = 'labels'
join   triage.labels    l on l.label_hash  = ra.artifact_id
group by ra.run_id, l.as_of_date, l.label_timespan;

alter table triage.experiments drop column task_framing;
"""


def upgrade():
    op.execute(UPGRADE_DDL)


def downgrade():
    op.execute(DOWNGRADE_DDL)
