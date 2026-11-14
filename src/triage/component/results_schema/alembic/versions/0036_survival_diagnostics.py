"""survival diagnostics: the persisted censoring-aware set, and events beside censored rows

The in-PG Harrell C-index (0011) ranks survival models, and audition keeps reading it. The
diagnostics a survival model needs beyond the ranking cannot be computed on read: Uno's C,
AUC(t) and Brier(t) weight each row by the training censoring distribution, and Brier(t)
and calibration read the estimator's survival function. Both come from the matrices and the
joblib file, so ``triage postmodel survival`` computes them once and persists them here, as
``triage postmodel`` does for error trees and crosstabs (ADR-0011).

``triage.survival_diagnostics`` holds one row per (model, split, date, kind, horizon, bin,
t). ``kind`` is one of:

* ``km_decile`` — the Kaplan-Meier event probability at the horizon per risk decile
  (``bin`` 1 is the riskiest; ``bin`` 0 is the population);
* ``km_group`` — the Kaplan-Meier curve of each risk tertile (``bin`` 0: the population),
  one row per step time ``t``;
* ``logrank`` — the log-rank p-value across the tertiles;
* ``calibration`` — the KM-observed survival at the horizon per decile of predicted
  survival, ``bin`` 0 holding ICI and E50;
* ``brier`` — Brier(t) over a time grid;
* ``auc_t`` — the cumulative/dynamic AUC(t) over the same grid;
* ``uno_c`` — Uno's C truncated at the horizon.

``value`` is NULL where the kind is undefined for the rows (no event, a time outside the
follow-up, an estimator without a survival function), and ``extra`` then says why.
``horizon`` and ``t`` are in the units of the label's ``duration``.

``model_score_histogram`` gains ``n_event`` and ``n_censored`` per bin for a survival
model (NULL for the other types), read from the labels each scored date is labeled with
(``label_source``, 0031). The new columns change the return type, so the 0029 function is
dropped first. Downgrade drops the table and restores the 0029 histogram.

Revision ID: 0036_survival_diagnostics
Revises: 0035_allocation_metrics
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

from alembic import op

revision = "0036_survival_diagnostics"
down_revision = "0035_allocation_metrics"
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

TABLE_DDL = r"""
create table triage.survival_diagnostics (
    model_id    bigint not null references triage.models(model_id) on delete cascade,
    split_kind  triage.split_kind not null,
    as_of_date  date   not null,
    kind        text   not null check (kind in ('km_decile', 'km_group', 'logrank',
                                                'calibration', 'brier', 'auc_t', 'uno_c')),
    horizon     double precision not null,   -- in the units of labels.duration
    bin         integer not null,             -- decile or tertile, 1 = riskiest; 0 = all rows
    t           double precision not null,    -- the time the value is read at
    value       double precision,             -- NULL: undefined here, extra says why
    n           integer,
    extra       jsonb,
    computed_at timestamptz not null default now(),
    primary key (model_id, split_kind, as_of_date, kind, horizon, bin, t)
);
"""

_HISTOGRAM_0029_START = (
    "-- Predicted-score histogram for the model card, over every test date."
)


def _histogram_0029() -> str:
    text = _revision_module("0029_typed_calibration").TYPED_DDL
    if text.count(_HISTOGRAM_0029_START) != 1:
        raise RuntimeError("the 0029 histogram anchor is not unique")
    return text[text.index(_HISTOGRAM_0029_START) :]


def _histogram_0036() -> str:
    """0029's histogram with the survival branch: events and censored rows per bin."""
    body = _histogram_0029()
    body = _replace_once(
        body,
        "-- scores all lie in [0, 1] is binned over [0, 1]; anything else over [min, max] of"
        " its scores.\n",
        "-- scores all lie in [0, 1] is binned over [0, 1]; anything else over [min, max] of"
        " its scores.\n"
        "-- A survival model's bins count the rows with an observed event and the censored"
        " ones.\n",
    )
    body = _replace_once(
        body,
        "              mean_outcome double precision)\n",
        "              mean_outcome double precision,\n"
        "              n_event      bigint,\n"
        "              n_censored   bigint)\n",
    )
    body = _replace_once(
        body,
        "  with s as materialized (\n"
        "    select sl.score, sl.outcome\n"
        "    from   triage.scored_labels(p_model, 'test') sl\n"
        "    where  sl.score is not null),\n"
        "  t as (\n"
        "    select triage.model_problem_type(p_model) = 'classification' as is_class),\n",
        "  with t as (\n"
        "    select pt = 'classification' as is_class, pt = 'survival' as is_survival\n"
        "    from   triage.model_problem_type(p_model) pt),\n"
        "  ev as materialized (\n"
        "    -- the label pair of a survival model's scored dates, as label_source pins it\n"
        "    select l.entity_id, l.as_of_date, l.event_observed\n"
        "    from   t\n"
        "    join   triage.label_source(p_model, 'test') ls on t.is_survival\n"
        "    join   triage.labels l\n"
        "           on  l.as_of_date     = ls.as_of_date\n"
        "           and l.label_timespan = ls.label_timespan\n"
        "           and l.label_hash     = ls.label_hash\n"
        "    where  l.duration is not null and l.event_observed is not null),\n"
        "  s as materialized (\n"
        "    select sl.score, sl.outcome, ev.event_observed\n"
        "    from   triage.scored_labels(p_model, 'test') sl\n"
        "    left   join ev on ev.entity_id = sl.entity_id and ev.as_of_date ="
        " sl.as_of_date\n"
        "    where  sl.score is not null),\n",
    )
    body = _replace_once(
        body,
        "           s.outcome\n    from   s cross join edges e)\n",
        "           s.outcome, s.event_observed\n    from   s cross join edges e)\n",
    )
    return _replace_once(
        body,
        "         avg(b.outcome)                                         as mean_outcome\n"
        "  from   b cross join edges e\n"
        "  group  by b.bin, e.lo, e.hi, e.is_class\n",
        "         avg(b.outcome)                                         as mean_outcome,\n"
        "         case when t.is_survival\n"
        "              then count(*) filter (where b.event_observed) end  as n_event,\n"
        "         case when t.is_survival\n"
        "              then count(*) filter (where not b.event_observed) end"
        " as n_censored\n"
        "  from   b cross join edges e cross join t\n"
        "  group  by b.bin, e.lo, e.hi, e.is_class, t.is_survival\n",
    )


_DROP_HISTOGRAM = "drop function triage.model_score_histogram(bigint, integer);"


def upgrade() -> None:
    op.execute(TABLE_DDL)
    op.execute(_DROP_HISTOGRAM)
    op.execute(_histogram_0036())


def downgrade() -> None:
    op.execute(_DROP_HISTOGRAM)
    op.execute(_histogram_0029())
    op.execute("drop table triage.survival_diagnostics;")
