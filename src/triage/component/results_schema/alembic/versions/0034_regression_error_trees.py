"""regression error trees: |error| leaves, beats-baseline leaves, and their suspicious form

The error trees of 0017/0027 are defined at a top-k cut, on a binary outcome. A regression model
has neither, so two kinds join them:

* ``abs_error`` — a regression tree on |ŷ − y| over every labeled row of a date. A leaf's
  ``error_rate`` is its mean |e| over the date's population MAE (1 is the average row), and
  ``n_errors`` counts its rows above that MAE. ``parameter`` is ``''``: there is no cut.
* ``beats_baseline`` — a classifier on "closer than the baseline" (strictly; a tie is not
  closer), fitted on the test dates before the model's last one and applied to the last.
  ``n_errors`` and ``error_rate`` count the training rows of the leaf where the model is not
  closer. ``parameter`` is ``'baseline:<model_group_id>'``.

``error_analysis.leaf_stats`` holds each kind's own numbers: ``n, mean_abs_error, bias,
share_of_abs_error, mean_outcome`` for ``abs_error``; ``train_n, train_win_share, test_n,
test_win_share, mean_gain, total_gain`` for ``beats_baseline``, where the gain is the
baseline's |e| minus the model's on the test rows of the leaf.

``triage.error_tree_fits`` records one fit per (model, split, kind, parameter): the dates it was
fitted on, the date it was applied to, the baseline, the AUC on that date and both base rates.

``error_rules_suspicious`` gains a regression branch and a ``p_mae_ratio`` argument: an
``abs_error`` leaf of at least ``p_min_support`` rows whose mean |e| is at most
``p_mae_ratio`` times the date's population MAE (from ``scored_labels``). Its lift is the
population MAE over the leaf's. Two columns join the result: ``mean_abs_error`` and
``population_mae``, NULL on classification leaves. The classification branch is unchanged.

Downgrade refuses while regression leaves exist, then restores the 0027 function and check.

Revision ID: 0034_regression_error_trees
Revises: 0033_regression_residuals
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from alembic import op

revision = "0034_regression_error_trees"
down_revision = "0033_regression_residuals"
branch_labels = None
depends_on = None


UPGRADE_DDL = r"""
alter table triage.error_analysis drop constraint error_analysis_error_kind_check;
alter table triage.error_analysis add constraint error_analysis_error_kind_check
    check (error_kind in ('fp', 'fn', 'confusion', 'abs_error', 'beats_baseline'));

alter table triage.error_analysis add column leaf_stats jsonb;

create table triage.error_tree_fits (
    model_id          bigint not null references triage.models(model_id) on delete cascade,
    split_kind        triage.split_kind not null,
    error_kind        text   not null,
    parameter         text   not null,
    fit_dates         date[] not null,
    eval_date         date,
    baseline_group_id bigint references triage.model_groups(model_group_id)
                             on delete cascade,
    auc               double precision,
    base_rate_train   double precision,
    base_rate_test    double precision,
    computed_at       timestamptz not null default now(),
    primary key (model_id, split_kind, error_kind, parameter)
);

drop function triage.error_rules_suspicious(bigint, double precision, integer);

-- Leaves that are suspiciously right. Classification (0027): fp leaves with an error rate near
-- zero, confusion leaves that are almost all TP, with lift over precision@ at the leaf's cut.
-- Regression: abs_error leaves whose mean |e| is a small fraction of the date's population
-- MAE, with lift = population MAE / leaf MAE. A large segment a forecaster gets almost exactly
-- right is as worth checking for leakage as a list segment that is all true positives.
create function triage.error_rules_suspicious(
    p_model          bigint,
    p_max_error_rate double precision default 0.02,
    p_min_support    integer default 30,
    p_mae_ratio      double precision default 0.1
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
    lift           double precision,
    mean_abs_error double precision,
    population_mae double precision
)
language sql stable as $$
    with population as (
        -- the date's MAE over the labeled rows, only for a model with abs_error leaves
        select s.as_of_date, avg(abs(s.score - s.outcome)) as mae
        from   triage.scored_labels(p_model, 'test') s
        where  s.outcome is not null and s.score is not null
          and  exists (select 1 from triage.error_analysis ea
                       where ea.model_id = p_model and ea.error_kind = 'abs_error')
        group  by s.as_of_date
    ),
    classification as (
        select ea.split_kind, ea.as_of_date, ea.parameter, ea.error_kind, ea.rule_id,
               ea.rule, ea.n_matched, ea.n_errors, ea.error_rate,
               (ea.class_counts ->> 'tp')::double precision
                   / nullif(ea.n_matched, 0)                    as tp_share,
               ea.class_counts, ea.signature,
               ev.value                                         as precision_at_k,
               (1 - ea.error_rate) / nullif(ev.value, 0)        as lift,
               null::double precision                           as mean_abs_error,
               null::double precision                           as population_mae
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
    ),
    regression as (
        select ea.split_kind, ea.as_of_date, ea.parameter, ea.error_kind, ea.rule_id,
               ea.rule, ea.n_matched, ea.n_errors, ea.error_rate,
               null::double precision, ea.class_counts, ea.signature,
               null::double precision,
               pop.mae / nullif((ea.leaf_stats ->> 'mean_abs_error')::double precision, 0),
               (ea.leaf_stats ->> 'mean_abs_error')::double precision,
               pop.mae
        from triage.error_analysis as ea
        join population pop on pop.as_of_date = ea.as_of_date
        where ea.model_id = p_model
          and ea.split_kind = 'test'
          and ea.error_kind = 'abs_error'
          and ea.n_matched >= p_min_support
          and (ea.leaf_stats ->> 'mean_abs_error')::double precision <= p_mae_ratio * pop.mae
    )
    select * from (
        select * from classification
        union all
        select * from regression
    ) leaves
    order by lift desc nulls last, n_matched desc, as_of_date, error_kind, rule_id
$$;
"""

DOWNGRADE_GUARD = r"""
do $$
begin
    if exists (select 1 from triage.error_analysis
               where error_kind in ('abs_error', 'beats_baseline')) then
        raise exception 'cannot downgrade below 0034: triage.error_analysis holds regression'
            ' leaves, which the 0027 check rejects — delete them first (delete from'
            ' triage.error_analysis where error_kind in (''abs_error'', ''beats_baseline''))';
    end if;
end
$$;

drop function triage.error_rules_suspicious(bigint, double precision, integer, double precision);
drop table triage.error_tree_fits;
alter table triage.error_analysis drop column leaf_stats;
alter table triage.error_analysis drop constraint error_analysis_error_kind_check;
alter table triage.error_analysis add constraint error_analysis_error_kind_check
    check (error_kind in ('fp', 'fn', 'confusion'));
"""


def _suspicious_0027() -> str:
    """0027's ``error_rules_suspicious``, read from that file rather than retyped."""
    path = Path(__file__).with_name("0027_error_confusion_recurrence.py")
    spec = importlib.util.spec_from_file_location("_triage_rev_0027", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load migration {path}")
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    text: str = loaded.UPGRADE_DDL
    start = "create function triage.error_rules_suspicious("
    end = "-- How often each rule signature recurs"
    for anchor in (start, end):
        if text.count(anchor) != 1:
            raise RuntimeError(f"anchor {anchor!r} occurs {text.count(anchor)} times")
    return text[text.index(start) : text.index(end)]


def upgrade() -> None:
    op.execute(UPGRADE_DDL)


def downgrade() -> None:
    op.execute(DOWNGRADE_GUARD)
    op.execute(_suspicious_0027())
