"""ranks keep split kinds apart; the curve is one test date at the scored label horizon

Two read paths disagreed with the stored evaluations.

* ``triage.latest_predictions`` kept one row per (model, entity, as_of_date) whatever its
  ``split_kind``, and ``triage.prediction_ranks`` ranked per (model, as_of_date). ``triage
  score`` appends ``split_kind='production'`` rows (ADR-0006), so a forward score at a test date
  hid the test score of every entity it covered, and a ``split_kind = 'test'`` filter on the
  view returned a list ranked together with production rows. Both views now carry
  ``split_kind`` in their key. Their columns do not change, and the consumers that already
  filter on ``split_kind`` (``list_overlap``, ``model_score_histogram``, the diagnostics) become
  correct without an edit.
* ``model_threshold_curve`` took every test row of the model and ordered them by their per-date
  ``rank_abs``, so a model tested on several as_of_dates drew one curve over interleaved lists;
  and it joined labels on ``models.training_label_timespan``, which finds nothing when the test
  matrix was labeled over a different horizon. It now takes ``p_as_of_date`` (NULL = the
  model's latest test date) and reads ``triage.population_ranks`` at the scored matrix's
  ``label_timespan`` — the same ranks, label pin and horizon ``precision@`` is computed from, so
  the curve at k agrees with the stored ``precision@k_abs`` by construction (ADR-0033). A test
  date whose predictions carry no matrix lineage falls back to the training horizon, which is
  what the curve used before.

The output columns of the curve are unchanged; the signature gains one defaulted argument, so a
one-argument call keeps working.

Revision ID: 0025_split_ranks_and_curve
Revises: 0024_population_topk
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

from alembic import op

revision = "0025_split_ranks_and_curve"
down_revision = "0024_population_topk"
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


def _between(text: str, start: str, end: str) -> str:
    """The slice of ``text`` from ``start`` up to ``end``, each found exactly once."""
    for anchor in (start, end):
        if text.count(anchor) != 1:
            raise RuntimeError(
                f"anchor {anchor[:60]!r} occurs {text.count(anchor)} times, expected once"
            )
    return text[text.index(start) : text.index(end)]


# --------------------------------------------------------------------------- upgrade DDL

VIEWS_DDL = r"""
-- Latest scoring run per (model, entity, as_of_date, split_kind): a production score never
-- replaces a test score of the same date.
create or replace view triage.latest_predictions as
select distinct on (model_id, entity_id, as_of_date, split_kind)
       model_id, entity_id, as_of_date, split_kind, score, scored_at
from   triage.predictions
order  by model_id, entity_id, as_of_date, split_kind, scored_at desc;

-- Prioritization list: deterministic-tiebreak ranks over the latest scores, one list per
-- (model, split_kind, as_of_date).
create or replace view triage.prediction_ranks as
select p.*,
       row_number()   over w as rank_abs,
       percent_rank() over w as rank_pct
from   triage.latest_predictions p
window w as (partition by model_id, split_kind, as_of_date order by score desc, entity_id);
"""

CURVE_DDL = r"""
drop function triage.model_threshold_curve(bigint);

-- The Rayid curve of one test date: population_ranks at the scored matrix's label horizon, so
-- the cut, its pct and the label pin are precision@'s own (ADR-0033). fp/fn/tn count labeled
-- rows. NULL p_as_of_date = the model's latest test date.
create function triage.model_threshold_curve(p_model bigint, p_as_of_date date default null)
returns table(k integer, pct double precision, prec double precision, rec double precision,
              tp bigint, fp bigint, fn bigint, tn bigint)
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
        select r.rank_abs, r.outcome
        from   triage.population_ranks(p_model, 'test', v_date, v_ts) r),
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
           (tot.l - tot.p - (cum.lab - cum.tp))::bigint       as tn
    from   cum cross join tot
    order  by cum.flagged;
end;
$$;
"""


def upgrade() -> None:
    op.execute(VIEWS_DDL)
    op.execute(CURVE_DDL)


# ------------------------------------------------------------------------------ downgrade


def _views_0001() -> str:
    """0001's two views, as written there, made replaceable."""
    views = _between(
        _revision_module("0001_initial_triage_schema").SCHEMA_DDL,
        "create view triage.latest_predictions as",
        "-- Leaderboard for dashboards; refresh after evaluations land.",
    )
    return views.replace("create view", "create or replace view")


def downgrade() -> None:
    op.execute("drop function triage.model_threshold_curve(bigint, date);")
    op.execute(_revision_module("0024_population_topk").CURVE_DDL)
    op.execute(_views_0001())
