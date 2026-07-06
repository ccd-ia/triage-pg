"""bias completeness — fnr/for/npv + config-driven fairness threshold (ADR-0007)

Revision ID: 0014_bias_completeness
Revises: 0013_audition_regret_next_time
Create Date: 2026-07-05

Closes the Aequitas metric gap (v1-release plan P2). ``compute_bias_metrics`` now emits
the full confusion-matrix group set — adding to {group_size, num_selected,
selection_rate, precision, tpr, fpr, fdr}:

* ``fnr`` = fn / group_pos                      (missed among the group's positives)
* ``for`` = fn / (group_size − num_selected)    (false omission rate, among not-selected)
* ``npv`` = tn / (group_size − num_selected)    (correct clearances among not-selected)

each with the same disparity-vs-reference semantics (explicit ``p_ref_groups`` pin, else
largest group). The fairness threshold moves from the frontend into SQL: new parameter
``p_tau`` (default 0.8, the four-fifths rule) stamps per-row ``tau`` and
``passes_fairness`` = disparity ∈ [τ, 1/τ] — NULL for count rows and NULL disparities,
so "no verdict" is distinguishable from "fails". Deliberately NOT added: a
``p_primary_metric`` parameter — which metric matters is an intervention-type question
(the fairness tree, docs/fairness.md) answered in config/UI, not per-row math.

Metric names follow Aequitas ('fnr', 'for', 'npv' — text values, no keyword clash).
Pre-0014 ``bias_metrics`` rows keep NULL tau/passes_fairness (honest: no τ was applied).
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "0014_bias_completeness"
down_revision = "0013_audition_regret_next_time"
branch_labels = None
depends_on = None


UPGRADE_DDL = r"""
alter table triage.bias_metrics
    add column tau              double precision,
    add column passes_fairness  boolean;

-- signature changes (p_tau appended): drop the 0002 overload first so callers can never
-- bind the stale six-argument version.
drop function if exists triage.compute_bias_metrics(
    bigint, triage.split_kind, date, interval, text, jsonb);

create or replace function triage.compute_bias_metrics(
    p_model_id        bigint,
    p_split_kind      triage.split_kind,
    p_as_of_date      date,
    p_label_timespan  interval,
    p_parameter       text,                      -- top-k threshold, e.g. '100_abs'
    p_ref_groups      jsonb default '{}'::jsonb, -- {"race": "White"} to pin reference; else largest group
    p_tau             double precision default 0.8 -- fairness threshold: pass = disparity in [tau, 1/tau]
)
returns integer
language plpgsql
volatile
as $$
declare
    n_labeled  integer;
    k          integer;
    written    integer := 0;
begin
    select count(*)::int
      into n_labeled
      from triage.labeled_ranks(p_model_id, p_split_kind, p_as_of_date, p_label_timespan);

    if n_labeled = 0 then
        return 0;
    end if;
    k := triage.resolve_k(p_parameter, n_labeled);

    -- Build the per-group metric table, then disparity vs the reference group,
    -- then unpivot to long format and upsert.
    with lr as (
        select entity_id, rank_abs, outcome
        from   triage.labeled_ranks(p_model_id, p_split_kind, p_as_of_date, p_label_timespan)
    ),
    joined as (
        select pg.attribute_name,
               pg.attribute_value,
               lr.outcome,
               (lr.rank_abs <= k) as selected
        from   lr
        join   triage.protected_groups pg
               on  pg.entity_id  = lr.entity_id
               and pg.as_of_date = p_as_of_date
    ),
    grouped as (
        select attribute_name,
               attribute_value,
               count(*)::double precision                                          as group_size,
               count(*) filter (where selected)::double precision                  as num_selected,
               count(*) filter (where selected and outcome > 0)::double precision   as tp,
               count(*) filter (where selected and outcome = 0)::double precision   as fp,
               count(*) filter (where not selected and outcome > 0)::double precision as fn,
               count(*) filter (where not selected and outcome = 0)::double precision as tn,
               count(*) filter (where outcome > 0)::double precision               as group_pos,
               count(*) filter (where outcome = 0)::double precision               as group_neg
        from   joined
        group  by attribute_name, attribute_value
    ),
    metrics as (
        select attribute_name, attribute_value,
               group_size,
               num_selected,
               case when group_size > 0 then num_selected / group_size end as selection_rate,
               case when num_selected > 0 then tp / num_selected end       as precision_ppv,
               case when group_pos   > 0 then tp / group_pos   end         as tpr,
               case when group_neg   > 0 then fp / group_neg   end         as fpr,
               case when num_selected > 0 then fp / num_selected end       as fdr,
               case when group_pos   > 0 then fn / group_pos   end         as fnr,
               case when (group_size - num_selected) > 0
                    then fn / (group_size - num_selected) end              as for_rate,
               case when (group_size - num_selected) > 0
                    then tn / (group_size - num_selected) end              as npv
        from   grouped
    ),
    -- reference group per attribute: explicit pin, else largest group_size
    refs as (
        select distinct on (m.attribute_name)
               m.attribute_name,
               coalesce(p_ref_groups->>m.attribute_name,
                        first_value(m.attribute_value) over (
                            partition by m.attribute_name
                            order by m.group_size desc, m.attribute_value)) as ref_value
        from   metrics m
        order  by m.attribute_name
    ),
    ref_vals as (
        select m.attribute_name,
               r.ref_value,
               m.selection_rate as ref_selection_rate,
               m.precision_ppv  as ref_precision,
               m.tpr            as ref_tpr,
               m.fpr            as ref_fpr,
               m.fdr            as ref_fdr,
               m.fnr            as ref_fnr,
               m.for_rate       as ref_for,
               m.npv            as ref_npv
        from   metrics m
        join   refs r on r.attribute_name = m.attribute_name
                     and m.attribute_value = r.ref_value
    ),
    long as (
        -- one row per (group, metric); disparity computed against the matching ref
        select m.attribute_name, m.attribute_value, x.metric, x.value,
               rv.ref_value,
               case
                 when x.metric = 'group_size'      then null  -- count, no disparity
                 when x.metric = 'num_selected'    then null
                 when x.metric = 'selection_rate' and rv.ref_selection_rate is not null
                      and rv.ref_selection_rate <> 0 then x.value / rv.ref_selection_rate
                 when x.metric = 'precision'      and rv.ref_precision is not null
                      and rv.ref_precision <> 0 then x.value / rv.ref_precision
                 when x.metric = 'tpr'            and rv.ref_tpr is not null
                      and rv.ref_tpr <> 0 then x.value / rv.ref_tpr
                 when x.metric = 'fpr'            and rv.ref_fpr is not null
                      and rv.ref_fpr <> 0 then x.value / rv.ref_fpr
                 when x.metric = 'fdr'            and rv.ref_fdr is not null
                      and rv.ref_fdr <> 0 then x.value / rv.ref_fdr
                 when x.metric = 'fnr'            and rv.ref_fnr is not null
                      and rv.ref_fnr <> 0 then x.value / rv.ref_fnr
                 when x.metric = 'for'            and rv.ref_for is not null
                      and rv.ref_for <> 0 then x.value / rv.ref_for
                 when x.metric = 'npv'            and rv.ref_npv is not null
                      and rv.ref_npv <> 0 then x.value / rv.ref_npv
                 else null
               end as disparity
        from   metrics m
        join   ref_vals rv on rv.attribute_name = m.attribute_name
        cross  join lateral (values
                   ('group_size',     m.group_size),
                   ('num_selected',   m.num_selected),
                   ('selection_rate', m.selection_rate),
                   ('precision',      m.precision_ppv),
                   ('tpr',            m.tpr),
                   ('fpr',            m.fpr),
                   ('fdr',            m.fdr),
                   ('fnr',            m.fnr),
                   ('for',            m.for_rate),
                   ('npv',            m.npv)
               ) as x(metric, value)
    )
    insert into triage.bias_metrics as b (
        model_id, split_kind, as_of_date, parameter,
        attribute_name, attribute_value, metric,
        value, ref_group_value, disparity, tau, passes_fairness, computed_at)
    select p_model_id, p_split_kind, p_as_of_date, p_parameter,
           attribute_name, attribute_value, metric,
           value, ref_value, disparity,
           case when disparity is not null then p_tau end,
           case when disparity is not null and p_tau is not null and p_tau > 0
                then disparity >= p_tau and disparity <= 1.0 / p_tau
           end,
           now()
    from   long
    on conflict (model_id, split_kind, as_of_date, parameter,
                 attribute_name, attribute_value, metric)
    do update set value = excluded.value,
                  ref_group_value = excluded.ref_group_value,
                  disparity = excluded.disparity,
                  tau = excluded.tau,
                  passes_fairness = excluded.passes_fairness,
                  computed_at = excluded.computed_at;

    get diagnostics written = row_count;
    return written;
end;
$$;
"""


# Restores the 0002 function verbatim and drops the τ columns.
DOWNGRADE_DDL = r"""
drop function if exists triage.compute_bias_metrics(
    bigint, triage.split_kind, date, interval, text, jsonb, double precision);

create or replace function triage.compute_bias_metrics(
    p_model_id        bigint,
    p_split_kind      triage.split_kind,
    p_as_of_date      date,
    p_label_timespan  interval,
    p_parameter       text,                     -- top-k threshold, e.g. '100_abs'
    p_ref_groups      jsonb default '{}'::jsonb -- {"race": "White"} to pin reference; else largest group
)
returns integer
language plpgsql
volatile
as $$
declare
    n_labeled  integer;
    k          integer;
    written    integer := 0;
begin
    select count(*)::int
      into n_labeled
      from triage.labeled_ranks(p_model_id, p_split_kind, p_as_of_date, p_label_timespan);

    if n_labeled = 0 then
        return 0;
    end if;
    k := triage.resolve_k(p_parameter, n_labeled);

    with lr as (
        select entity_id, rank_abs, outcome
        from   triage.labeled_ranks(p_model_id, p_split_kind, p_as_of_date, p_label_timespan)
    ),
    joined as (
        select pg.attribute_name,
               pg.attribute_value,
               lr.outcome,
               (lr.rank_abs <= k) as selected
        from   lr
        join   triage.protected_groups pg
               on  pg.entity_id  = lr.entity_id
               and pg.as_of_date = p_as_of_date
    ),
    grouped as (
        select attribute_name,
               attribute_value,
               count(*)::double precision                                          as group_size,
               count(*) filter (where selected)::double precision                  as num_selected,
               count(*) filter (where selected and outcome > 0)::double precision   as tp,
               count(*) filter (where selected and outcome = 0)::double precision   as fp,
               count(*) filter (where not selected and outcome > 0)::double precision as fn,
               count(*) filter (where outcome > 0)::double precision               as group_pos,
               count(*) filter (where outcome = 0)::double precision               as group_neg
        from   joined
        group  by attribute_name, attribute_value
    ),
    metrics as (
        select attribute_name, attribute_value,
               group_size,
               num_selected,
               case when group_size > 0 then num_selected / group_size end as selection_rate,
               case when num_selected > 0 then tp / num_selected end       as precision_ppv,
               case when group_pos   > 0 then tp / group_pos   end         as tpr,
               case when group_neg   > 0 then fp / group_neg   end         as fpr,
               case when num_selected > 0 then fp / num_selected end       as fdr
        from   grouped
    ),
    refs as (
        select distinct on (m.attribute_name)
               m.attribute_name,
               coalesce(p_ref_groups->>m.attribute_name,
                        first_value(m.attribute_value) over (
                            partition by m.attribute_name
                            order by m.group_size desc, m.attribute_value)) as ref_value
        from   metrics m
        order  by m.attribute_name
    ),
    ref_vals as (
        select m.attribute_name,
               r.ref_value,
               m.selection_rate as ref_selection_rate,
               m.precision_ppv  as ref_precision,
               m.tpr            as ref_tpr,
               m.fpr            as ref_fpr,
               m.fdr            as ref_fdr
        from   metrics m
        join   refs r on r.attribute_name = m.attribute_name
                     and m.attribute_value = r.ref_value
    ),
    long as (
        select m.attribute_name, m.attribute_value, x.metric, x.value,
               rv.ref_value,
               case
                 when x.metric = 'group_size'      then null
                 when x.metric = 'num_selected'    then null
                 when x.metric = 'selection_rate' and rv.ref_selection_rate is not null
                      and rv.ref_selection_rate <> 0 then x.value / rv.ref_selection_rate
                 when x.metric = 'precision'      and rv.ref_precision is not null
                      and rv.ref_precision <> 0 then x.value / rv.ref_precision
                 when x.metric = 'tpr'            and rv.ref_tpr is not null
                      and rv.ref_tpr <> 0 then x.value / rv.ref_tpr
                 when x.metric = 'fpr'            and rv.ref_fpr is not null
                      and rv.ref_fpr <> 0 then x.value / rv.ref_fpr
                 when x.metric = 'fdr'            and rv.ref_fdr is not null
                      and rv.ref_fdr <> 0 then x.value / rv.ref_fdr
                 else null
               end as disparity
        from   metrics m
        join   ref_vals rv on rv.attribute_name = m.attribute_name
        cross  join lateral (values
                   ('group_size',     m.group_size),
                   ('num_selected',   m.num_selected),
                   ('selection_rate', m.selection_rate),
                   ('precision',      m.precision_ppv),
                   ('tpr',            m.tpr),
                   ('fpr',            m.fpr),
                   ('fdr',            m.fdr)
               ) as x(metric, value)
    )
    insert into triage.bias_metrics as b (
        model_id, split_kind, as_of_date, parameter,
        attribute_name, attribute_value, metric,
        value, ref_group_value, disparity, computed_at)
    select p_model_id, p_split_kind, p_as_of_date, p_parameter,
           attribute_name, attribute_value, metric,
           value, ref_value, disparity, now()
    from   long
    on conflict (model_id, split_kind, as_of_date, parameter,
                 attribute_name, attribute_value, metric)
    do update set value = excluded.value,
                  ref_group_value = excluded.ref_group_value,
                  disparity = excluded.disparity,
                  computed_at = excluded.computed_at;

    get diagnostics written = row_count;
    return written;
end;
$$;

alter table triage.bias_metrics
    drop column if exists passes_fairness,
    drop column if exists tau;
"""


def upgrade():
    op.execute(UPGRADE_DDL)


def downgrade():
    op.execute(DOWNGRADE_DDL)
