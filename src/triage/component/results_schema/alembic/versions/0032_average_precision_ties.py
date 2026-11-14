"""average precision treats a block of tied scores as one threshold, as sklearn does

``average_precision`` (0015) numbered the labeled rows by ``score desc, entity_id`` and summed
the precision at each positive's own rank. Within a block of tied scores that rank depends on
the entity ids, which the model never saw, so the value moved with them, and it differed from
``sklearn.metrics.average_precision_score`` whenever scores tied: by 0.10 on a test fixture of
six distinct scores. A tree, a calibrated bucketed score or a rounded baseline ties most rows.

The function now steps through the distinct scores, highest first. At each one the whole tie
block enters the list:

    AP = Σ_t (pos_t / P) · (TP_t / N_t)

where ``pos_t`` is the block's positives, ``P`` all labeled positives, and ``TP_t``, ``N_t``
the positives and rows scoring at least ``t``. Without ties this is the previous value.

Stored ``average_precision`` rows are recomputed in place at the label horizon of the scored
matrix, the way 0024 recomputed the threshold metrics. A key whose predictions carry no matrix
lineage has no recoverable horizon and keeps its value; the NOTICE counts those keys. The
leaderboard is refreshed after.

Downgrade restores the 0015 function. It does not restore the old values.

Revision ID: 0032_average_precision_ties
Revises: 0031_common_rows_and_coverage
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from alembic import op

revision = "0032_average_precision_ties"
down_revision = "0031_common_rows_and_coverage"
branch_labels = None
depends_on = None


def _between(module: str, name: str, start: str, end: str) -> str:
    """The slice of an earlier revision's DDL constant from ``start`` up to ``end``."""
    path = Path(__file__).with_name(f"{module}.py")
    spec = importlib.util.spec_from_file_location(f"_triage_rev_{module}", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load migration {path}")
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    text: str = getattr(loaded, name)
    for anchor in (start, end):
        if text.count(anchor) != 1:
            raise RuntimeError(
                f"anchor {anchor[:60]!r} occurs {text.count(anchor)} times, expected once"
            )
    return text[text.index(start) : text.index(end)]


AVERAGE_PRECISION_DDL = r"""
create or replace function triage.average_precision(
    p_model_id       bigint,
    p_split_kind     triage.split_kind,
    p_as_of_date     date,
    p_label_timespan interval,
    p_subset_hash    text default ''
)
returns triage.metric_result
language plpgsql
stable
as $$
declare
    r           triage.metric_result;
    n_labeled   integer;
    n_positive  integer;
begin
    select count(*)::int, coalesce(sum((outcome > 0)::int), 0)::int
      into n_labeled, n_positive
      from triage.labeled_ranks(p_model_id, p_split_kind, p_as_of_date, p_label_timespan, p_subset_hash);

    r.num_labeled  := n_labeled;
    r.num_positive := n_positive;

    if n_positive = 0 then
        return r;
    end if;

    -- One threshold per distinct score, highest first: the whole tie block enters at once.
    with blocks as (
        select score,
               count(*)                as n,
               sum((outcome > 0)::int) as pos
        from   triage.labeled_ranks(p_model_id, p_split_kind, p_as_of_date, p_label_timespan, p_subset_hash)
        group  by score
    ),
    cum as (
        select pos,
               sum(n)   over (order by score desc) as n_cum,
               sum(pos) over (order by score desc) as tp_cum
        from   blocks
    )
    select sum(pos * (tp_cum::double precision / n_cum)) / n_positive
      into r.value
      from cum;

    return r;
end;
$$;
"""

RECOMPUTE_DDL = r"""
do $$
declare
    key       record;
    ts        interval;
    n_ts      integer;
    res       triage.metric_result;
    n_rows    integer := 0;
    n_skipped integer := 0;
begin
    for key in
        select model_id, split_kind, as_of_date, subset_hash
          from triage.evaluations
         where metric = 'average_precision'
    loop
        -- the label_timespan the scored matrix was built with
        select count(distinct mx.label_timespan), min(mx.label_timespan)
          into n_ts, ts
          from triage.predictions pr
          join triage.matrices mx on mx.matrix_uuid = pr.matrix_uuid
         where pr.model_id   = key.model_id
           and pr.split_kind = key.split_kind
           and pr.as_of_date = key.as_of_date;
        if n_ts <> 1 then
            n_skipped := n_skipped + 1;
            continue;
        end if;
        res := triage.average_precision(key.model_id, key.split_kind, key.as_of_date, ts,
                                        key.subset_hash);
        update triage.evaluations e
           set value = res.value,
               num_labeled = res.num_labeled,
               num_positive = res.num_positive,
               computed_at = now()
         where e.model_id = key.model_id and e.split_kind = key.split_kind
           and e.as_of_date = key.as_of_date and e.subset_hash = key.subset_hash
           and e.metric = 'average_precision' and e.parameter = '';
        n_rows := n_rows + 1;
    end loop;

    raise notice 'migration 0032: recomputed % average_precision row(s);'
        ' % without matrix lineage left as they were', n_rows, n_skipped;
end;
$$;

refresh materialized view triage.leaderboard;
"""


def upgrade() -> None:
    op.execute(AVERAGE_PRECISION_DDL)
    op.execute(RECOMPUTE_DDL)


def downgrade() -> None:
    op.execute(
        _between(
            "0015_subset_evaluation",
            "METRICS_DDL",
            "create or replace function triage.average_precision(",
            "drop function if exists triage.regression_metric(bigint, triage.split_kind,"
            " date, interval, text);",
        )
    )
