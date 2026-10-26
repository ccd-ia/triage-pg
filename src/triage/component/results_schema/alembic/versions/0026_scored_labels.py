"""score reads label each prediction from its scored matrix, as its evaluation did

``model_score_histogram`` and the dashboard's predicted list joined labels through the run's
labels artifact at ``models.training_label_timespan``. Two things went wrong:

* a test matrix labeled over another horizon than the training one matched no label, so the
  histogram drew no positives and the list showed every outcome as unknown;
* the histogram inner-joined ``run_artifacts``, so a run holding two labels artifacts counted
  every prediction twice.

``triage.scored_labels(model, split_kind)`` is the one place both now read: the
``prediction_ranks`` rows of one split, each labeled at its scored matrix's ``label_timespan``
from the labels artifact that matrix was built from — the pin and horizon ``population_ranks``
uses for ``precision@`` (ADR-0033). A date whose predictions carry no matrix lineage falls back
to what these reads used before: the training horizon and the run's labels artifact. A date that
mixes two horizons or two labels artifacts raises instead of picking one.

Revision ID: 0026_scored_labels
Revises: 0025_split_ranks_and_curve
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

from alembic import op

revision = "0026_scored_labels"
down_revision = "0025_split_ranks_and_curve"
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

SCORED_LABELS_DDL = r"""
-- prediction_ranks of one split, each row labeled from its scored matrix's lineage.
create function triage.scored_labels(
    p_model      bigint,
    p_split_kind triage.split_kind default 'test'
)
returns table (
    entity_id  bigint,
    as_of_date date,
    score      double precision,
    rank_abs   bigint,
    rank_pct   double precision,
    outcome    double precision
)
language plpgsql
stable
as $$
#variable_conflict use_column
declare
    d          record;
    v_run_pin  text;
    v_training interval;
begin
    -- the fallback for a date without matrix lineage: what these reads used before 0026
    select (select min(ra.artifact_id)
              from triage.run_artifacts ra
              join triage.artifacts a on a.artifact_id = ra.artifact_id and a.kind = 'labels'
             where ra.run_id = m.run_id),
           m.training_label_timespan
      into v_run_pin, v_training
      from triage.models m
     where m.model_id = p_model;

    for d in
        select pr.as_of_date,
               count(distinct mx.label_timespan) as n_ts,
               min(mx.label_timespan)            as ts,
               count(distinct la.artifact_id)    as n_pins,
               min(la.artifact_id)               as pin
          from triage.predictions pr
          left join triage.matrices        mx on mx.matrix_uuid = pr.matrix_uuid
          left join triage.artifact_inputs ai on ai.artifact_id = mx.artifact_id
          left join triage.artifacts       la on la.artifact_id = ai.parent_id
                                             and la.kind = 'labels'
         where pr.model_id = p_model and pr.split_kind = p_split_kind
         group by pr.as_of_date
         order by pr.as_of_date
    loop
        if d.n_ts > 1 or d.n_pins > 1 then
            raise exception 'model % at %: the % predictions span % label timespans and % labels'
                ' artifacts — refusing to mix label definitions',
                p_model, d.as_of_date, p_split_kind, d.n_ts, d.n_pins;
        end if;

        return query
        select pr.entity_id, pr.as_of_date, pr.score, pr.rank_abs, pr.rank_pct, l.outcome
        from   triage.prediction_ranks pr
        left join triage.labels l
               on  l.entity_id      = pr.entity_id
               and l.as_of_date     = pr.as_of_date
               and l.label_timespan = coalesce(d.ts, v_training)
               and l.label_hash     = coalesce(d.pin, v_run_pin)
        where  pr.model_id   = p_model
          and  pr.split_kind = p_split_kind
          and  pr.as_of_date = d.as_of_date;
    end loop;
end;
$$;

-- predicted-score histogram (by class) for the model card, over every test date.
create or replace function triage.model_score_histogram(p_model bigint, p_bins integer default 20)
returns table(bin integer, lo double precision, hi double precision, n bigint, n_pos bigint)
language sql stable as $$
  with b as (
    select width_bucket(s.score, 0.0, 1.0, p_bins) as bin, s.outcome
    from   triage.scored_labels(p_model, 'test') s
    where  s.score is not null)
  select b.bin,
         (b.bin - 1)::double precision / p_bins as lo,
         (b.bin)::double precision     / p_bins as hi,
         count(*)                               as n,
         count(*) filter (where b.outcome = 1)  as n_pos
  from   b group by b.bin order by b.bin;
$$;
"""


def upgrade() -> None:
    op.execute(SCORED_LABELS_DDL)


# ------------------------------------------------------------------------------ downgrade


def _histogram_0005() -> str:
    return _between(
        _revision_module("0005_experiment_dashboard").UPGRADE_DDL,
        "-- predicted-score histogram (by class) for the model card.",
        "-- --------------------------------------------------------------- ontology source",
    )


def downgrade() -> None:
    op.execute(_histogram_0005())
    op.execute("drop function triage.scored_labels(bigint, triage.split_kind);")
