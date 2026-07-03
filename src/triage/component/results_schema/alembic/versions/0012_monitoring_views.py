"""monitoring layer over append-only predictions (ADR-0006 payoff, ADR-0027)

Revision ID: 0012_monitoring_views
Revises: 0011_survival_c_index
Create Date: 2026-07-03

The ADR-0006 design bet — append-only, ``scored_at``-timestamped, partitioned predictions —
pays off here as plain SQL:

* ``monitoring_volume`` (view) — scoring heartbeat per (model group, model, split, day).
* ``monitoring_score_drift(...)`` (function) — PSI (reference-decile bins, ε-smoothed) + KS
  between a pinned reference window and a scoring window; windows are parameters so the
  pinned-reference policy (ADR-0027) is a convention, not a schema.
* ``monitoring_calibration(...)`` (function) — score-decile vs realized outcome rate over the
  artifact-pinned ``labeled_ranks`` (0011).
* ``monitoring_outcome_tracking`` (view) — realized metrics over time: ``evaluations`` rows
  (idempotent upserts — re-run ``evaluate_model`` when labels arrive) sequenced per model
  group with each run's ``purpose`` (experiment / retrain / forward_score, ADR-0018).
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "0012_monitoring_views"
down_revision = "0011_survival_c_index"
branch_labels = None
depends_on = None


MONITORING_DDL = r"""
-- ------------------------------------------------ volume heartbeat
create view triage.monitoring_volume as
select mg.model_group_id,
       m.model_id,
       p.split_kind,
       p.scored_at::date as scored_on,
       count(*)                 as n_predictions,
       count(distinct p.entity_id) as n_entities,
       min(p.as_of_date)        as first_as_of_date,
       max(p.as_of_date)        as last_as_of_date
from   triage.predictions p
join   triage.models       m  using (model_id)
join   triage.model_groups mg using (model_group_id)
group  by mg.model_group_id, m.model_id, p.split_kind, p.scored_at::date;

-- ------------------------------------------------ score drift (PSI + KS)
-- Reference bins are the REFERENCE window's score deciles; PSI = Σ (p_w - p_r)·ln(p_w/p_r)
-- with ε-smoothing so an empty bin cannot produce ±infinity. KS = max |ECDF_r - ECDF_w|.
create or replace function triage.monitoring_score_drift(
    p_model_group_id bigint,
    p_reference_from timestamptz,
    p_reference_to   timestamptz,
    p_window_from    timestamptz,
    p_window_to      timestamptz
)
returns table (
    psi          double precision,
    ks           double precision,
    n_reference  bigint,
    n_window     bigint
)
language sql
stable
as $$
    with ref as materialized (
        select p.score
        from   triage.predictions p
        join   triage.models m using (model_id)
        where  m.model_group_id = p_model_group_id
          and  p.scored_at >= p_reference_from and p.scored_at < p_reference_to
    ),
    win as materialized (
        select p.score
        from   triage.predictions p
        join   triage.models m using (model_id)
        where  m.model_group_id = p_model_group_id
          and  p.scored_at >= p_window_from and p.scored_at < p_window_to
    ),
    -- decile edges from the REFERENCE distribution (drift is measured against what we
    -- validated); 9 inner edges -> 10 bins, degenerate edges collapse harmlessly.
    edges as (
        select percentile_cont(array[0.1,0.2,0.3,0.4,0.5,0.6,0.7,0.8,0.9])
                 within group (order by score) as qs
        from ref
    ),
    binned as (
        select b.bin,
               count(r.score) as n_ref,
               (select count(*) from win w
                 where width_bucket(w.score, e.qs) + 1 = b.bin) as n_win
        from edges e
        cross join generate_series(1, 10) as b(bin)
        left join ref r on width_bucket(r.score, e.qs) + 1 = b.bin
        group by b.bin, e.qs
    ),
    totals as (
        select (select count(*) from ref)::double precision as t_ref,
               (select count(*) from win)::double precision as t_win
    ),
    psi_calc as (
        select sum(
                 (
                   ((n_win / nullif(t_win, 0)) + 1e-6)
                   - ((n_ref / nullif(t_ref, 0)) + 1e-6)
                 )
                 * ln(
                     ((n_win / nullif(t_win, 0)) + 1e-6)
                     / ((n_ref / nullif(t_ref, 0)) + 1e-6)
                   )
               ) as psi
        from binned, totals
    ),
    -- KS: ECDFs evaluated at each DISTINCT pooled score (post-tie aggregation, so tied
    -- scores across the two samples step together — the scipy.stats.ks_2samp definition).
    pooled as (
        select score, 1 as is_ref, 0 as is_win from ref
        union all
        select score, 0, 1 from win
    ),
    per_score as (
        select score,
               sum(is_ref) as c_ref,
               sum(is_win) as c_win
        from pooled
        group by score
    ),
    ecdf as (
        select sum(c_ref) over (order by score) / nullif(t_ref, 0) as f_ref,
               sum(c_win) over (order by score) / nullif(t_win, 0) as f_win
        from per_score, totals
    ),
    ks_calc as (
        select max(abs(coalesce(f_ref, 0) - coalesce(f_win, 0))) as ks from ecdf
    )
    select psi_calc.psi,
           ks_calc.ks,
           (select count(*) from ref),
           (select count(*) from win)
    from psi_calc, ks_calc;
$$;

-- ------------------------------------------------ calibration (deciles vs realized rate)
create or replace function triage.monitoring_calibration(
    p_model_id       bigint,
    p_split_kind     triage.split_kind,
    p_as_of_date     date,
    p_label_timespan interval
)
returns table (
    decile        integer,
    n             bigint,
    avg_score     double precision,
    realized_rate double precision
)
language sql
stable
as $$
    with ranked as materialized (
        select score, outcome,
               ntile(10) over (order by score desc) as decile
        from triage.labeled_ranks(p_model_id, p_split_kind, p_as_of_date, p_label_timespan)
    )
    select decile,
           count(*)                              as n,
           avg(score)                            as avg_score,
           avg((outcome > 0)::int)::double precision as realized_rate
    from ranked
    group by decile
    order by decile;
$$;

-- ------------------------------------------------ realized metrics over time
-- evaluations upsert idempotently per (model, split, date, metric, parameter): when labels
-- arrive for a forward-scored date, re-running evaluate_model writes the REALIZED row; this
-- sequences them per model group, tagged with the owning run's purpose (ADR-0018).
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
    op.execute(MONITORING_DDL)


def downgrade():
    op.execute(r"""
        drop view if exists triage.monitoring_outcome_tracking;
        drop function if exists triage.monitoring_calibration(bigint, triage.split_kind, date, interval);
        drop function if exists triage.monitoring_score_drift(bigint, timestamptz, timestamptz, timestamptz, timestamptz);
        drop view if exists triage.monitoring_volume;
        """)
