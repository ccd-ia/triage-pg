"""near miss and prototypes: the rows just below a cut, and the most typical row of each class

Three classification diagnostics (they read regression_ranking too, where positive means
``outcome > 0``):

* ``triage.near_miss(model, split, as_of_date, parameter, width_parameter)`` — three bands
  of the population ranking (ADR-0033): ``selected`` (rank ≤ k), ``near_miss``
  (k < rank ≤ k + w) and ``rest``, with k = ``resolve_k(parameter, n)`` and
  w = ``resolve_k(width_parameter, n)`` over the n scored rows. A NULL width is the cut
  itself, so the near-miss band is as large as the list. Each band reports its rows, its
  labeled rows, the positive share and the mean outcome of the labeled ones, and the mean
  score of all of them. Computed on read from ``population_ranks`` at the label horizon
  ``label_source`` gives the date.
* ``triage.crosstabs.comparison`` — what the selected rows are compared with: ``'rest'``
  (every row below the cut, the only comparison before this revision) or ``'near_miss'``
  (the next k rows). It joins the primary key, so a near-miss crosstab and a rest crosstab
  of one cut sit side by side. Existing rows read ``'rest'``.
* ``triage.prototypes`` — per (model, split, date, cut, class at the cut), the entities
  nearest the class centroid in z-scored feature space, with their largest deviations from
  it. ``triage postmodel prototypes`` computes them from the scored matrix once and
  persists them here (ADR-0011).

Downgrade drops the function and the table, deletes the near-miss crosstabs (a newer
triage-pg recomputes them) and restores the 0017 primary key.

Revision ID: 0037_near_miss_and_prototypes
Revises: 0036_survival_diagnostics
"""

from __future__ import annotations

from alembic import op

revision = "0037_near_miss_and_prototypes"
down_revision = "0036_survival_diagnostics"
branch_labels = None
depends_on = None


NEAR_MISS_DDL = r"""
-- The rows just below a top-k cut beside the rows inside it, on one scored date. The cut and
-- the width are resolve_k over every scored row (population_ranks, ADR-0033); the rates count
-- the labeled rows of each band, and positive means outcome > 0. A band with no row has NULL
-- ranks and rates.
create function triage.near_miss(
    p_model           bigint,
    p_split_kind      triage.split_kind,
    p_as_of_date      date,
    p_parameter       text,
    p_width_parameter text default null   -- NULL: as wide as the cut
)
returns table (
    band         text,
    band_ix      integer,
    first_rank   integer,
    last_rank    integer,
    n            integer,
    n_labeled    integer,
    n_positive   integer,
    base_rate    double precision,
    mean_outcome double precision,
    mean_score   double precision
)
language plpgsql
stable
as $$
#variable_conflict use_column
declare
    v_ts interval;
begin
    select ls.label_timespan into v_ts
      from triage.label_source(p_model, p_split_kind) ls
     where ls.as_of_date = p_as_of_date;
    if v_ts is null then
        return;  -- the model scored nothing on that date
    end if;

    return query
    with pop as materialized (
        select pr.rank_abs, pr.score, pr.outcome
          from triage.population_ranks(p_model, p_split_kind, p_as_of_date, v_ts, '') pr
    ),
    cut as (
        select count(*)::int as n_pop,
               triage.resolve_k(p_parameter, count(*)::int) as k,
               triage.resolve_k(coalesce(p_width_parameter, p_parameter), count(*)::int)
                   as w
          from pop
    ),
    bounds as (
        select b.band, b.band_ix, b.lo, b.hi
          from cut c
         cross join lateral (
               values ('selected',  1, 1,                             c.k),
                      ('near_miss', 2, c.k + 1,                       least(c.k + c.w, c.n_pop)),
                      ('rest',      3, least(c.k + c.w, c.n_pop) + 1, c.n_pop)
               ) b(band, band_ix, lo, hi)
    )
    select b.band,
           b.band_ix,
           case when b.lo <= b.hi then b.lo end,
           case when b.lo <= b.hi then b.hi end,
           count(pop.rank_abs)::int,
           count(pop.outcome)::int,
           (count(*) filter (where pop.outcome > 0))::int,
           (count(*) filter (where pop.outcome > 0))::double precision
               / nullif(count(pop.outcome), 0),
           avg(pop.outcome),
           avg(pop.score)
      from bounds b
      left join pop on pop.rank_abs between b.lo and b.hi
     group by b.band, b.band_ix, b.lo, b.hi
     order by b.band_ix;
end;
$$;
"""

CROSSTABS_DDL = r"""
alter table triage.crosstabs
    add column comparison text not null default 'rest'
        check (comparison in ('rest', 'near_miss'));
comment on column triage.crosstabs.comparison is
    'What the selected top-k is compared with: rest (every row below the cut) or near_miss'
    ' (the next k rows). rest_value holds that comparison''s value.';
alter table triage.crosstabs drop constraint crosstabs_pkey;
alter table triage.crosstabs
    add primary key (model_id, split_kind, as_of_date, parameter, comparison, feature, stat);
"""

PROTOTYPES_DDL = r"""
create table triage.prototypes (
    model_id    bigint not null references triage.models(model_id) on delete cascade,
    split_kind  triage.split_kind not null,
    as_of_date  date   not null,
    parameter   text   not null,               -- the top-k cut, e.g. '100_abs'
    class       text   not null check (class in ('tp', 'fp', 'fn', 'tn')),
    class_size  integer not null,              -- the labeled rows of the class on the date
    rank        integer not null,              -- 1 = nearest the class centroid
    entity_id   bigint not null,
    distance    double precision not null,     -- Euclidean, in z-scored feature space
    deviations  jsonb  not null,               -- the largest |z - centroid|, largest first
    computed_at timestamptz not null default now(),
    primary key (model_id, split_kind, as_of_date, parameter, class, rank)
);
"""

CROSSTABS_DOWNGRADE_DDL = r"""
delete from triage.crosstabs where comparison <> 'rest';
alter table triage.crosstabs drop constraint crosstabs_pkey;
alter table triage.crosstabs drop column comparison;
alter table triage.crosstabs
    add primary key (model_id, split_kind, as_of_date, parameter, feature, stat);
"""


def upgrade() -> None:
    op.execute(NEAR_MISS_DDL)
    op.execute(CROSSTABS_DDL)
    op.execute(PROTOTYPES_DDL)


def downgrade() -> None:
    op.execute("drop table triage.prototypes;")
    op.execute(CROSSTABS_DOWNGRADE_DDL)
    op.execute(
        "drop function triage.near_miss(bigint, triage.split_kind, date, text, text);"
    )
