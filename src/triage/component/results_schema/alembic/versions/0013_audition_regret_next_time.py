"""audition regret-next-time — the last Python-audition capability, in SQL (ADR-0007)

Revision ID: 0013_audition_regret_next_time
Revises: 0012_monitoring_views
Create Date: 2026-07-05

Closes the audition dual-surface flag (docs/adr-conformance.md #1): with this column the
SQL surface carries everything the retired ``component/audition`` computed. DSSG
semantics (``distance_from_best.py``): ``dist_from_best_case_next_time`` for a model
group at time t is the regret that group realizes at the NEXT evaluated time — "if I
committed to this group after seeing t, how far from best would I be at t+1". NULL on
each group's last split. ``triage.audition`` gains the ``avg/max_regret_next_time``
aggregates the selection conversation actually uses.

Also hardens the audition base for the subset era: only full-cohort evaluation rows
(``subset_hash = ''``) feed ``audition_distances``. Behavior-neutral today (subset
*filtering* ships in migration 0015 — every existing row has ``subset_hash = ''``),
load-bearing after: subset-scoped evaluations must never contaminate the ranking.

``audition_pick()`` / ``selected_model()`` are plpgsql/SQL-string functions (late-bound)
and keep working unchanged over the recreated views.
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "0013_audition_regret_next_time"
down_revision = "0012_monitoring_views"
branch_labels = None
depends_on = None


UPGRADE_DDL = r"""
drop view if exists triage.audition;
drop view if exists triage.audition_distances;

create view triage.audition_distances as
with base as (
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
    where  e.split_kind = 'test' and e.value is not null and e.subset_hash = ''
    window w as (partition by r.experiment_hash, e.metric, e.parameter, e.as_of_date)
)
select base.*,
       lead(dist_from_best_case) over (
           partition by experiment_hash, model_group_id, metric, parameter
           order by as_of_date
       ) as dist_from_best_case_next_time
from base;

create view triage.audition as
select experiment_hash, metric, parameter, model_group_id,
       count(*)                  as n_splits_evaluated,
       avg(raw_value)            as avg_value,
       stddev_samp(raw_value)    as stddev_value,
       avg(dist_from_best_case)  as avg_distance_from_best,
       max(dist_from_best_case)  as max_regret,
       avg(dist_from_best_case_next_time) as avg_regret_next_time,
       max(dist_from_best_case_next_time) as max_regret_next_time
from   triage.audition_distances
group by experiment_hash, metric, parameter, model_group_id;
"""


# The 0005 definitions, verbatim.
DOWNGRADE_DDL = r"""
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
"""


def upgrade():
    op.execute(UPGRADE_DDL)


def downgrade():
    op.execute(DOWNGRADE_DDL)
