"""allocation metrics: lift@k, captured@k, AUCC, Spearman, and the gains curve

A ``regression_ranking`` model ranks entities so that a budget of k goes where the outcome is
largest. rmse and mae score the forecast, not the allocation. This revision adds the allocation
set:

* ``lift@<k>`` — the mean outcome of the labeled rows in the top k over the mean outcome of every
  labeled row. ``captured@<k>`` — the outcome in the top k over the total outcome. Both take the
  ``precision@`` thresholds (``100_abs``, ``10_pct``) and cut the population ranking (ADR-0033):
  every scored row is ranked, ties broken by entity_id, and the sums run over the labeled rows.
  ``num_labeled_above_threshold`` holds the labeled rows inside the cut.
* ``aucc`` — the area under the cumulative-gains curve: x is the share of the population
  selected, y the share of the outcome captured, by the trapezoid rule. A block of tied scores is
  one segment of the curve (the expectation over the orders of the block), so a constant score
  gives 0.5. ``spearman`` — the rank correlation of score and outcome over the labeled rows,
  ties at their average rank; NULL when either is constant.
* ``gains_curve(model, split, as_of_date, points)`` — ``points + 1`` rows from k = 0 to the
  whole population, with ``captured``, ``lift`` and the top-k mean outcome.

lift, captured and AUCC are shares of a total, so they are NULL when a labeled outcome is
negative or the total is 0: the 0023 rule for a value that does not apply. The rows are still
written, with their ``num_labeled``.

``evaluate_model`` reads ``lift@`` and ``captured@`` in the ``metrics`` list beside
``precision@``, and ``aucc`` and ``spearman`` in ``regression_metrics``. All four are higher is
better, which ``higher_is_better`` already returns for a name it does not list.

Downgrade restores the 0024 ``evaluate_model`` and the 0030 ``regression_metric`` and drops the
two new functions. Stored rows of the four metrics are deleted first, since nothing could
recompute them.

Revision ID: 0035_allocation_metrics
Revises: 0034_regression_error_trees
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

from alembic import op

revision = "0035_allocation_metrics"
down_revision = "0034_regression_error_trees"
branch_labels = None
depends_on = None


def _revision_module(module: str) -> ModuleType:
    """An earlier revision, loaded from its file (revision files are not importable by name)."""
    path = Path(__file__).with_name(f"{module}.py")
    spec = importlib.util.spec_from_file_location(f"_triage_rev_{module}", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load migration {path}")
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


def _replace_once(text: str, old: str, new: str) -> str:
    if text.count(old) != 1:
        raise RuntimeError(
            f"anchor {old[:60]!r} occurs {text.count(old)} times, expected once"
        )
    return text.replace(old, new)


# --------------------------------------------------------------------------- upgrade DDL

ALLOCATION_DDL = r"""
-- lift@k and captured@k at a population top-k cut (ADR-0033): the cut ranks every scored row,
-- the sums run over the labeled rows inside it.
create function triage.allocation_at_k(
    p_model_id       bigint,
    p_split_kind     triage.split_kind,
    p_as_of_date     date,
    p_label_timespan interval,
    p_metric         text,          -- 'lift@' | 'captured@'
    p_parameter      text,
    p_subset_hash    text default ''
)
returns triage.metric_result
language plpgsql
stable
as $$
declare
    r          triage.metric_result;
    n_pop      integer;
    n_labeled  integer;
    total      double precision;
    signed     boolean;
    k          integer;
    m          integer;
    top_sum    double precision;
begin
    if p_metric not in ('lift@', 'captured@') then
        raise exception 'unknown allocation metric % (expected lift@|captured@)', p_metric;
    end if;

    select count(*)::int, count(outcome)::int, sum(outcome), coalesce(bool_or(outcome < 0), false)
      into n_pop, n_labeled, total, signed
      from triage.population_ranks(p_model_id, p_split_kind, p_as_of_date, p_label_timespan,
                                   p_subset_hash);

    r.num_labeled := n_labeled;
    if n_labeled = 0 then
        return r;
    end if;

    k := triage.resolve_k(p_parameter, n_pop);
    select count(outcome)::int, coalesce(sum(outcome), 0)
      into m, top_sum
      from triage.population_ranks(p_model_id, p_split_kind, p_as_of_date, p_label_timespan,
                                   p_subset_hash)
     where rank_abs <= k;
    r.num_labeled_above_threshold := m;

    -- a share of a total: undefined for a signed outcome or a zero total (the 0023 rule)
    if signed or total = 0 then
        return r;
    end if;
    if p_metric = 'captured@' then
        r.value := top_sum / total;
    elsif m > 0 then
        r.value := (top_sum / m) / (total / n_labeled);
    end if;
    return r;
end;
$$;

-- The cumulative-gains curve of one date: points + 1 cuts from k = 0 to the population,
-- k = ceil(i / points · N) as resolve_k reads a share. NULL captured and lift where a labeled
-- outcome is negative or the total is 0.
create function triage.gains_curve(
    p_model      bigint,
    p_split_kind triage.split_kind,
    p_as_of_date date,
    p_points     integer default 100
)
returns table (
    k_pct             double precision,
    k                 integer,
    n_labeled_topk    integer,
    captured          double precision,
    lift              double precision,
    mean_outcome_topk double precision
)
language plpgsql
stable
as $$
#variable_conflict use_column
declare
    v_ts interval;
begin
    if p_points is null or p_points < 1 then
        raise exception 'gains_curve needs at least one point, got %', p_points;
    end if;
    select ls.label_timespan into v_ts
      from triage.label_source(p_model, p_split_kind) ls
     where ls.as_of_date = p_as_of_date;
    if v_ts is null then
        return;  -- the model scored nothing on that date
    end if;

    return query
    with pop as materialized (
        select pr.rank_abs, pr.outcome
          from triage.population_ranks(p_model, p_split_kind, p_as_of_date, v_ts, '') pr
    ),
    tot as (
        select count(*)::int as n_pop, count(outcome)::int as n_labeled,
               sum(outcome) as total, coalesce(bool_or(outcome < 0), false) as signed
          from pop
    ),
    cum as (
        -- the labeled outcome and the labeled count over ranks 1..rank_abs
        select rank_abs,
               coalesce(sum(outcome) over (order by rank_abs), 0) as top_sum,
               count(outcome) over (order by rank_abs)            as top_n
          from pop
    ),
    cuts as (
        select i::double precision / p_points as k_pct,
               ceil(i::numeric / p_points * t.n_pop)::int as k
          from generate_series(0, p_points) i, tot t
    )
    select c.k_pct,
           c.k,
           coalesce(cu.top_n, 0)::int,
           case when not t.signed and t.total <> 0
                then coalesce(cu.top_sum, 0) / nullif(t.total, 0) end,
           case when not t.signed and t.total <> 0 and cu.top_n > 0
                then (cu.top_sum / cu.top_n) / nullif(t.total / t.n_labeled, 0) end,
           case when cu.top_n > 0 then cu.top_sum / cu.top_n end
      from cuts c
     cross join tot t
      left join cum cu on cu.rank_abs = c.k
     order by c.k_pct;
end;
$$;
"""

_UNKNOWN_REGRESSION = (
    "    else\n"
    "        raise exception 'unknown regression metric % (expected"
    " rmse|mae|r2|wape|pinball@<tau>)', p_metric;\n"
)

_AUCC_SPEARMAN = r"""    elsif p_metric = 'aucc' then
        -- Area under the cumulative-gains curve over the population ranking. Each block of
        -- tied scores is one segment: its trapezoid runs from the gains before the block to
        -- the gains after it.
        if exists (select 1
                     from triage.labeled_ranks(p_model_id, p_split_kind, p_as_of_date,
                                               p_label_timespan, p_subset_hash)
                    where outcome < 0) then
            return r;
        end if;
        with pop as (
            select score, coalesce(outcome, 0) as y
              from triage.population_ranks(p_model_id, p_split_kind, p_as_of_date,
                                           p_label_timespan, p_subset_hash)
        ),
        blocks as (
            select score, count(*) as n, sum(y) as y from pop group by score
        ),
        cum as (
            select n, y, sum(y) over (order by score desc) as cy from blocks
        ),
        totals as (select count(*) as n_pop, sum(y) as total from pop)
        -- nullif, not a case: the aggregate would be evaluated before the case
        select sum(c.n::double precision / t.n_pop
                   * ((c.cy - c.y) + c.cy) / (2 * nullif(t.total, 0)))
          into r.value
          from cum c, totals t
         group by t.n_pop, t.total;
    elsif p_metric = 'spearman' then
        -- Pearson's correlation of the average ranks: Spearman's rho with ties.
        with lr as (
            select score, outcome
              from triage.labeled_ranks(p_model_id, p_split_kind, p_as_of_date,
                                        p_label_timespan, p_subset_hash)
        ),
        ranked as (
            select rank() over (order by score)
                       + (count(*) over (partition by score) - 1) / 2.0 as rs,
                   rank() over (order by outcome)
                       + (count(*) over (partition by outcome) - 1) / 2.0 as ro
              from lr
        )
        select corr(rs, ro) into r.value from ranked;
    else
        raise exception 'unknown regression metric % (expected'
            ' rmse|mae|r2|wape|pinball@<tau>|aucc|spearman)', p_metric;
"""


def _regression_metric_0035() -> str:
    body = _revision_module("0030_wape_and_forecast_quantile").REGRESSION_METRIC_WAPE
    body = _replace_once(
        body,
        "    p_metric         text,          -- 'rmse' | 'mae' | 'r2' | 'wape' | 'pinball@<tau>'",
        "    p_metric         text,          -- 'rmse' | 'mae' | 'r2' | 'wape' | 'pinball@<tau>'"
        " | 'aucc' | 'spearman'",
    )
    return _replace_once(body, _UNKNOWN_REGRESSION, _AUCC_SPEARMAN)


def _evaluate_model_0024() -> str:
    return _revision_module("0024_population_topk")._evaluate_model_0024()


def _evaluate_model_0035() -> str:
    """0024's evaluate_model with lift@ and captured@ in the threshold branch."""
    body = _evaluate_model_0024()
    body = _replace_once(
        body,
        "        if v_metric in ('precision@', 'recall@') then\n",
        "        if v_metric in ('precision@', 'recall@', 'lift@', 'captured@') then\n",
    )
    body = _replace_once(
        body,
        "                else\n"
        "                    res := triage.recall_at_k(p_model_id, p_split_kind, p_as_of_date,\n"
        "                                              p_label_timespan, v_threshold,"
        " p_subset_hash);\n",
        "                elsif v_metric = 'recall@' then\n"
        "                    res := triage.recall_at_k(p_model_id, p_split_kind, p_as_of_date,\n"
        "                                              p_label_timespan, v_threshold,"
        " p_subset_hash);\n"
        "                else\n"
        "                    res := triage.allocation_at_k(p_model_id, p_split_kind,"
        " p_as_of_date,\n"
        "                                                  p_label_timespan, v_metric,"
        " v_threshold,\n"
        "                                                  p_subset_hash);\n",
    )
    return _replace_once(
        body,
        "(expected precision@|recall@|auc_roc|",
        "(expected precision@|recall@|lift@|captured@|auc_roc|",
    )


def upgrade() -> None:
    op.execute(ALLOCATION_DDL)
    op.execute(_regression_metric_0035())
    op.execute(_evaluate_model_0035())


def downgrade() -> None:
    op.execute(
        "delete from triage.evaluations"
        " where metric in ('lift@', 'captured@', 'aucc', 'spearman');"
    )
    op.execute(_evaluate_model_0024())
    op.execute(
        _revision_module("0030_wape_and_forecast_quantile").REGRESSION_METRIC_WAPE
    )
    op.execute(
        "drop function triage.gains_curve(bigint, triage.split_kind, date, integer);"
        " drop function triage.allocation_at_k(bigint, triage.split_kind, date, interval,"
        " text, text, text);"
        " refresh materialized view triage.leaderboard;"
    )
