"""error trees — a four-class confusion tree, structured leaf conditions, two read functions

``triage.error_analysis`` (migration 0017) holds one row per leaf of the fp and fn error trees.
This revision makes those leaves comparable across dates and searchable:

* ``error_kind = 'confusion'`` — a third tree, fitted on every labeled row at the cut with the
  four classes TP / FP / FN / TN as its target. The fp and fn trees are conditioned on selection;
  this one is not, so its first splits mostly redraw the selection boundary.
* ``class_counts`` (``{"tp": .., "fp": ..}`` on fp leaves, ``{"fn": .., "tn": ..}`` on fn leaves,
  all four on confusion leaves), ``conditions`` (the path as
  ``[{"feature": .., "op": "<=", "threshold": ..}]``) and ``signature`` (the sorted, distinct
  ``feature op`` pairs without thresholds, e.g. ``facility_age>|inspections_1y<=``; ``''`` for a
  root-only tree). The signature is what lets a rule be recognised on another date, where the
  thresholds differ. All three are nullable: rows written before this revision keep NULLs until
  the tree is re-run, because re-running replaces a date's rows.
* ``triage.error_rules_suspicious(model, max_error_rate, min_support)`` — the leaves that are
  suspiciously right: fp leaves with high support and an error rate near zero, and confusion
  leaves that are almost all TP. ``lift`` is the leaf's ``1 - error_rate`` over the model's
  ``precision@`` at the same date and cut (NULL when that cut was not evaluated).
* ``triage.error_rule_recurrence(model_group, parameter, error_kind)`` — on how many of a model
  group's test dates each signature appears.

Revision ID: 0027_error_confusion_recurrence
Revises: 0026_scored_labels
"""

from __future__ import annotations

from alembic import op

# id <= 32 chars: results_schema_versions.version_num
revision = "0027_error_confusion_recurrence"
down_revision = "0026_scored_labels"
branch_labels = None
depends_on = None


UPGRADE_DDL = r"""
alter table triage.error_analysis drop constraint error_analysis_error_kind_check;
alter table triage.error_analysis add constraint error_analysis_error_kind_check
    check (error_kind in ('fp', 'fn', 'confusion'));

alter table triage.error_analysis
    add column class_counts jsonb,
    add column conditions   jsonb,
    add column signature    text;

-- Leaves that are suspiciously right: high support and (almost) no error. On a list ranked by a
-- model that does not see the future, a large segment of the selected rows that is nearly all
-- true positives is worth a second look — it is a leakage signature as often as a real signal.
-- fp leaves qualify on their error rate; confusion leaves on their TP share. lift compares the
-- leaf's 1 - error_rate with the model's precision@ at the same date and cut.
create function triage.error_rules_suspicious(
    p_model          bigint,
    p_max_error_rate double precision default 0.02,
    p_min_support    integer default 30
)
returns table (
    split_kind     triage.split_kind,
    as_of_date     date,
    parameter      text,
    error_kind     text,
    rule_id        integer,
    rule           text,
    n_matched      integer,
    n_errors       integer,
    error_rate     double precision,
    tp_share       double precision,
    class_counts   jsonb,
    signature      text,
    precision_at_k double precision,
    lift           double precision
)
language sql stable as $$
    select ea.split_kind,
           ea.as_of_date,
           ea.parameter,
           ea.error_kind,
           ea.rule_id,
           ea.rule,
           ea.n_matched,
           ea.n_errors,
           ea.error_rate,
           (ea.class_counts ->> 'tp')::double precision
               / nullif(ea.n_matched, 0)                    as tp_share,
           ea.class_counts,
           ea.signature,
           ev.value                                         as precision_at_k,
           (1 - ea.error_rate) / nullif(ev.value, 0)        as lift
    from triage.error_analysis as ea
    left join triage.evaluations as ev
           on ev.model_id = ea.model_id
          and ev.split_kind = ea.split_kind
          and ev.as_of_date = ea.as_of_date
          and ev.subset_hash = ''
          and ev.metric = 'precision@'
          and ev.parameter = ea.parameter
    where ea.model_id = p_model
      and ea.n_matched >= p_min_support
      and (
            (ea.error_kind = 'fp' and ea.error_rate <= p_max_error_rate)
         or (ea.error_kind = 'confusion'
             and (ea.class_counts ->> 'tp')::double precision
                 >= (1 - p_max_error_rate) * ea.n_matched)
      )
    order by lift desc nulls last, ea.n_matched desc, ea.as_of_date, ea.error_kind,
             ea.rule_id
$$;

-- How often each rule signature recurs across a model group's test dates. n_dates_total counts
-- every (model, date) analysed at this cut and kind, including rows written before 0027 that
-- carry no signature yet, so a rule found on 2 of 3 dates may be on all 3 once those are re-run.
create function triage.error_rule_recurrence(
    p_model_group bigint,
    p_parameter   text,
    p_error_kind  text default 'fp'
)
returns table (
    signature       text,
    n_dates         integer,
    n_dates_total   integer,
    mean_error_rate double precision,
    mean_support    double precision,
    example_rule    text
)
language sql stable as $$
    with rows as (
        select ea.*
        from triage.error_analysis as ea
        inner join triage.models as m using (model_id)
        where m.model_group_id = p_model_group
          and ea.parameter = p_parameter
          and ea.error_kind = p_error_kind
          and ea.split_kind = 'test'
    ), total as (
        select count(distinct (model_id, as_of_date)) as n from rows
    )
    select signature,
           count(distinct (model_id, as_of_date))::integer  as n_dates,
           (select n from total)::integer                  as n_dates_total,
           avg(error_rate)                                 as mean_error_rate,
           avg(n_matched)::double precision                as mean_support,
           (array_agg(rule order by n_matched desc))[1]    as example_rule
    from rows
    where signature is not null
    group by signature
    order by n_dates desc, mean_error_rate desc, signature
$$;
"""

DOWNGRADE_DDL = r"""
do $$
begin
    if exists (select 1 from triage.error_analysis where error_kind = 'confusion') then
        raise exception 'cannot downgrade below 0027: triage.error_analysis holds'
            ' error_kind = ''confusion'' rows, which the 0017 check rejects — delete'
            ' error_kind = ''confusion'' rows first'
            ' (delete from triage.error_analysis where error_kind = ''confusion'')';
    end if;
end
$$;

drop function if exists triage.error_rule_recurrence(bigint, text, text);
drop function if exists triage.error_rules_suspicious(bigint, double precision, integer);

alter table triage.error_analysis
    drop column if exists signature,
    drop column if exists conditions,
    drop column if exists class_counts;

alter table triage.error_analysis drop constraint error_analysis_error_kind_check;
alter table triage.error_analysis add constraint error_analysis_error_kind_check
    check (error_kind in ('fp', 'fn'));
"""


def upgrade():
    op.execute(UPGRADE_DDL)


def downgrade():
    op.execute(DOWNGRADE_DDL)
