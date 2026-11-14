"""calibration and the score histogram read the problem type, and tied scores share a decile

``monitoring_calibration`` (0012) split the labeled rows with ``ntile(10)`` and reported
``avg((outcome > 0)::int)``. Both assume a classifier with spread-out scores:

* a regression model's outcome is a quantity, so the share of rows above zero says nothing
  about whether its forecasts are calibrated;
* when most rows share one score (a tree that gives 84% of rows the same forecast),
  ``ntile`` spreads that block over several deciles in arbitrary order, and their means
  compare rows the model could not tell apart.

The function now takes ``p_bins`` (default 10) and returns, per decile, ``mean_outcome``
(every type), ``lift`` (the decile's mean outcome over the population's; NULL when that is
0), ``realized_rate`` (classification only, NULL otherwise) and ``n_distinct_scores``. The
decile is ``ceil(cume_dist() · bins)`` over the score order, computed in integers, so every
row of a tied block lands in the decile of the block's last row, and a decile the block
covers is absent.

``score_ties(model, split, as_of_date)`` reports the largest block of tied scores, so a
caller can say why deciles are missing.

``model_score_histogram`` bins over ``[0, 1]`` only for a classifier whose scores all fall
there; otherwise over ``[min(score), max(score)]``. ``n_pos`` is NULL for the other types,
and every bin gains ``mean_outcome``. A score of exactly the upper edge lands in the last
bin instead of a bin of its own above the range.

``model_problem_type(model)`` reads the type through ``models → runs → experiments``; a
model with no run lineage reads as ``classification``, what both functions assumed before.

New output columns change the return types, so the old functions are dropped first, as
0025 and 0028 did. Downgrade restores the 0012 calibration and the 0026 histogram.

Revision ID: 0029_typed_calibration
Revises: 0028_subset_threshold_curve
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

from alembic import op

revision = "0029_typed_calibration"
down_revision = "0028_subset_threshold_curve"
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


def _count_once(text: str, anchor: str) -> None:
    if text.count(anchor) != 1:
        raise RuntimeError(
            f"anchor {anchor[:60]!r} occurs {text.count(anchor)} times, expected once"
        )


def _between(text: str, start: str, end: str) -> str:
    """The slice of ``text`` from ``start`` up to ``end``, each found exactly once."""
    for anchor in (start, end):
        _count_once(text, anchor)
    return text[text.index(start) : text.index(end)]


def _from(text: str, start: str) -> str:
    """The slice of ``text`` from ``start`` to its end; ``start`` must occur exactly once."""
    _count_once(text, start)
    return text[text.index(start) :]


# --------------------------------------------------------------------------- upgrade DDL

TYPED_DDL = r"""
drop function triage.monitoring_calibration(bigint, triage.split_kind, date, interval);
drop function triage.model_score_histogram(bigint, integer);

-- The problem type a model was trained for. A model with no run lineage (a hand-seeded
-- model) reads as classification: what calibration and the histogram assumed before 0029.
create function triage.model_problem_type(p_model bigint)
returns triage.problem_type
language sql stable as $$
    select coalesce(
        (select e.problem_type
           from triage.models      m
           join triage.runs        r on r.run_id = m.run_id
           join triage.experiments e on e.experiment_hash = r.experiment_hash
          where m.model_id = p_model),
        'classification'::triage.problem_type);
$$;

-- Reliability deciles over the labeled rows of one (model, split, date). The decile is
-- ceil(cume_dist() * bins) over the score order: c, the rows scoring at least this row's
-- score (its peers included), gives ceil(c * bins / N), computed in integers so a boundary
-- row never rounds into the next decile. Tied rows share a decile; an empty one is absent.
create function triage.monitoring_calibration(
    p_model_id       bigint,
    p_split_kind     triage.split_kind,
    p_as_of_date     date,
    p_label_timespan interval,
    p_subset_hash    text    default '',
    p_bins           integer default 10
)
returns table (
    decile            integer,
    n                 bigint,
    avg_score         double precision,
    mean_outcome      double precision,
    lift              double precision,
    realized_rate     double precision,
    n_distinct_scores bigint
)
language plpgsql
stable
as $$
#variable_conflict use_column
declare
    v_type triage.problem_type := triage.model_problem_type(p_model_id);
begin
    if p_bins is null or p_bins < 1 then
        raise exception 'monitoring_calibration: bins must be 1 or more, got %', p_bins;
    end if;

    return query
    with lr as materialized (
        select score, outcome
        from triage.labeled_ranks(p_model_id, p_split_kind, p_as_of_date, p_label_timespan,
                                  p_subset_hash)
    ),
    counted as (
        select score, outcome,
               count(*) over (order by score desc) as c,   -- peers included (RANGE frame)
               count(*) over ()                    as total
        from lr
    ),
    ranked as (
        select score, outcome,
               least(p_bins::bigint, (c * p_bins + total - 1) / total)::integer as decile
        from counted
    ),
    overall as (
        select avg(outcome) as mean from lr
    )
    select r.decile,
           count(*)                                       as n,
           avg(r.score)                                   as avg_score,
           avg(r.outcome)                                 as mean_outcome,
           avg(r.outcome) / nullif(o.mean, 0)             as lift,
           case when v_type = 'classification'
                then avg((r.outcome > 0)::int)::double precision end as realized_rate,
           count(distinct r.score)                        as n_distinct_scores
    from ranked r
    cross join overall o
    group by r.decile, o.mean
    order by r.decile;
end;
$$;

-- The largest block of tied scores among the latest scores of one (model, split, date):
-- why calibration deciles go missing. Ties between blocks of one size go to the higher score.
create function triage.score_ties(
    p_model_id   bigint,
    p_split_kind triage.split_kind,
    p_as_of_date date
)
returns table (
    n                 bigint,
    n_distinct        bigint,
    largest_tie_n     bigint,
    largest_tie_score double precision,
    largest_tie_share double precision
)
language sql
stable
as $$
    with p as materialized (
        select lp.score
        from triage.latest_predictions lp
        where lp.model_id   = p_model_id
          and lp.split_kind = p_split_kind
          and lp.as_of_date = p_as_of_date
          and lp.score is not null
    ),
    blocks as (
        select score, count(*) as k from p group by score
    ),
    largest as (
        select score, k from blocks order by k desc, score desc limit 1
    )
    select (select count(*) from p),
           (select count(*) from blocks),
           largest.k,
           largest.score,
           largest.k::double precision / nullif((select count(*) from p), 0)
    from (select 1) as one
    left join largest on true;
$$;

-- Predicted-score histogram for the model card, over every test date. A classifier whose
-- scores all lie in [0, 1] is binned over [0, 1]; anything else over [min, max] of its scores.
create function triage.model_score_histogram(p_model bigint, p_bins integer default 20)
returns table(bin          integer,
              lo           double precision,
              hi           double precision,
              n            bigint,
              n_pos        bigint,
              mean_outcome double precision)
language sql stable as $$
  with s as materialized (
    select sl.score, sl.outcome
    from   triage.scored_labels(p_model, 'test') sl
    where  sl.score is not null),
  t as (
    select triage.model_problem_type(p_model) = 'classification' as is_class),
  r as (
    select t.is_class, min(s.score) as smin, max(s.score) as smax,
           t.is_class and min(s.score) >= 0 and max(s.score) <= 1 as unit
    from   s cross join t
    group  by t.is_class),
  edges as (
    select r.is_class,
           case when r.unit then 0.0::double precision else r.smin end as lo,
           case when r.unit then 1.0::double precision else r.smax end as hi
    from   r),
  b as (
    select case when e.hi = e.lo then 1
                else least(p_bins, width_bucket(s.score, e.lo, e.hi, p_bins)) end as bin,
           s.outcome
    from   s cross join edges e)
  select b.bin,
         e.lo + (b.bin - 1) * (e.hi - e.lo) / p_bins            as lo,
         case when b.bin = p_bins then e.hi
              else e.lo + b.bin * (e.hi - e.lo) / p_bins end     as hi,
         count(*)                                               as n,
         case when e.is_class
              then count(*) filter (where b.outcome = 1) end    as n_pos,
         avg(b.outcome)                                         as mean_outcome
  from   b cross join edges e
  group  by b.bin, e.lo, e.hi, e.is_class
  order  by b.bin;
$$;
"""


def upgrade() -> None:
    op.execute(TYPED_DDL)


# ------------------------------------------------------------------------------ downgrade


def _calibration_0012() -> str:
    return _between(
        _revision_module("0012_monitoring_views").MONITORING_DDL,
        "-- ------------------------------------------------ calibration (deciles vs realized rate)",
        "-- ------------------------------------------------ realized metrics over time",
    )


def _histogram_0026() -> str:
    return _from(
        _revision_module("0026_scored_labels").SCORED_LABELS_DDL,
        "-- predicted-score histogram (by class) for the model card, over every test date.",
    )


def downgrade() -> None:
    op.execute(
        "drop function triage.model_score_histogram(bigint, integer);"
        " drop function triage.score_ties(bigint, triage.split_kind, date);"
        " drop function triage.monitoring_calibration("
        "     bigint, triage.split_kind, date, interval, text, integer);"
    )
    op.execute(_calibration_0012())
    op.execute(_histogram_0026())
    op.execute("drop function triage.model_problem_type(bigint);")
