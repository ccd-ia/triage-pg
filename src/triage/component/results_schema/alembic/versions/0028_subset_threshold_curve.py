"""the Rayid curve of a subset, with the base rate of the population it is drawn over

``model_threshold_curve`` drew one test date over the whole scored population, so a subgroup
whose curve is flat — ranked as a block against everyone else but not within itself — could
not be seen. It now takes ``p_subset_hash`` (``''`` = everyone, the convention
``triage.evaluations`` uses). Within a subset the subset is the population, as it is for the
subset metrics (0015): ``population_ranks`` keeps the subset's members before its
``row_number()``, so ``k``, ``pct``, the label pin and the horizon are subset ``precision@``'s
own, and the curve at k agrees with the stored subset ``precision@k_abs`` by construction
(ADR-0033).

The output gains ``base_rate``: labeled positives over labeled rows of the population the curve
is drawn over — the precision a random ranking expects, and the curve's own precision at
k = N. The dashboard draws it as a reference line, and ``triage model curve`` prints it.

With ``p_subset_hash = ''`` every earlier column is what the previous function returned, row for
row. A new output column changes the return type, so the two-argument function is dropped
first, as 0025 did; one- and two-argument calls resolve through the defaults.

Revision ID: 0028_subset_threshold_curve
Revises: 0027_error_confusion_recurrence
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

from alembic import op

revision = "0028_subset_threshold_curve"
down_revision = "0027_error_confusion_recurrence"
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


def _from(text: str, start: str) -> str:
    """The slice of ``text`` from ``start`` to its end; ``start`` must occur exactly once."""
    if text.count(start) != 1:
        raise RuntimeError(
            f"anchor {start[:60]!r} occurs {text.count(start)} times, expected once"
        )
    return text[text.index(start) :]


# --------------------------------------------------------------------------- upgrade DDL

CURVE_DDL = r"""
drop function triage.model_threshold_curve(bigint, date);

-- The Rayid curve of one test date: population_ranks at the scored matrix's label horizon, so
-- the cut, its pct and the label pin are precision@'s own (ADR-0033). fp/fn/tn count labeled
-- rows. NULL p_as_of_date = the model's latest test date. A non-empty p_subset_hash makes the
-- subset the population (0015): its members are ranked among themselves. base_rate is the
-- labeled positives over the labeled rows of that population.
create function triage.model_threshold_curve(
    p_model       bigint,
    p_as_of_date  date default null,
    p_subset_hash text default ''
)
returns table(k integer, pct double precision, prec double precision, rec double precision,
              tp bigint, fp bigint, fn bigint, tn bigint, base_rate double precision)
language plpgsql stable as $$
#variable_conflict use_column
declare
    v_date date := p_as_of_date;
    v_ts   interval;
    v_n_ts integer;
begin
    if v_date is null then
        select max(pr.as_of_date) into v_date
          from triage.predictions pr
         where pr.model_id = p_model and pr.split_kind = 'test';
        if v_date is null then
            return;
        end if;
    end if;

    -- the label_timespan the scored matrix was built with (0024's recompute reads it the same way)
    select count(distinct mx.label_timespan), min(mx.label_timespan)
      into v_n_ts, v_ts
      from triage.predictions pr
      join triage.matrices mx on mx.matrix_uuid = pr.matrix_uuid
     where pr.model_id = p_model and pr.split_kind = 'test' and pr.as_of_date = v_date;
    if v_n_ts > 1 then
        raise exception 'model % at %: the test predictions span % label timespans —'
            ' the curve refuses to mix label definitions', p_model, v_date, v_n_ts;
    end if;
    if v_ts is null then
        -- no matrix lineage: the training horizon, as the curve used before 0025
        select m.training_label_timespan into v_ts
          from triage.models m where m.model_id = p_model;
    end if;

    return query
    with ranked as (
        -- the subset filter and the re-rank inside it are population_ranks' (0015 semantics)
        select r.rank_abs, r.outcome
        from   triage.population_ranks(p_model, 'test', v_date, v_ts, p_subset_hash) r),
    tot as (
        select count(*) as n, count(ranked.outcome) as l,
               coalesce(sum(ranked.outcome), 0) as p
        from   ranked),
    cum as (
        select ranked.rank_abs                                                  as flagged,
               coalesce(sum(ranked.outcome) over (order by ranked.rank_abs), 0) as tp,
               count(ranked.outcome) over (order by ranked.rank_abs)            as lab
        from   ranked)
    select cum.flagged::int                                   as k,
           cum.flagged::double precision / nullif(tot.n, 0)   as pct,
           cum.tp / nullif(cum.lab, 0)                        as prec,
           cum.tp / nullif(tot.p, 0)                          as rec,
           cum.tp::bigint                                     as tp,
           (cum.lab - cum.tp)::bigint                         as fp,
           (tot.p - cum.tp)::bigint                           as fn,
           (tot.l - tot.p - (cum.lab - cum.tp))::bigint       as tn,
           tot.p / nullif(tot.l, 0)                           as base_rate
    from   cum cross join tot
    order  by cum.flagged;
end;
$$;
"""


def upgrade() -> None:
    op.execute(CURVE_DDL)


# ------------------------------------------------------------------------------ downgrade


def _curve_0025() -> str:
    """0025's two-argument curve as written there, without the drop that precedes it."""
    return _from(
        _revision_module("0025_split_ranks_and_curve").CURVE_DDL,
        "-- The Rayid curve of one test date:",
    )


def downgrade() -> None:
    op.execute("drop function triage.model_threshold_curve(bigint, date, text);")
    op.execute(_curve_0025())
