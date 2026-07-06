"""postmodeling diagnostics, part 1 — list overlap + per-model train duration (plan P4)

Revision ID: 0016_diagnostics
Revises: 0015_subset_evaluation
Create Date: 2026-07-06

Two small additions the model↔model-group contrast surfaces need:

* ``triage.list_overlap(model_a, model_b, parameter, split_kind, as_of_date?)`` — do two
  models flag the same entities? Per shared prediction date: each model's top-k (its own
  ``resolve_k`` — populations can differ), the intersection, Jaccard over the union of
  the two lists, and a Spearman rank correlation over ALL commonly-scored entities
  (Pearson over ``rank_abs`` pairs ≡ Spearman). Pure SQL over ``prediction_ranks`` —
  no labels needed, so it works on unlabeled/forward-scored dates too.

* ``triage.models.train_duration_ms`` — wall-clock fit time, recorded by the adapter
  around ``estimator.fit``. NULL for pre-0016 models (honest unknown) and for
  cache-reclaimed rows (the original fit's duration stands).
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "0016_diagnostics"
down_revision = "0015_subset_evaluation"
branch_labels = None
depends_on = None


UPGRADE_DDL = r"""
alter table triage.models add column train_duration_ms bigint;

create or replace function triage.list_overlap(
    p_model_a    bigint,
    p_model_b    bigint,
    p_parameter  text,
    p_split_kind triage.split_kind default 'test',
    p_as_of_date date default null
)
returns table (
    as_of_date     date,
    k_a            integer,
    k_b            integer,
    n_intersection bigint,
    jaccard        double precision,
    rank_corr      double precision
)
language plpgsql
stable
as $$
declare
    d date;
    v_n_a integer;
    v_n_b integer;
begin
    for d in
        select pr.as_of_date from triage.prediction_ranks pr
        where pr.model_id = p_model_a and pr.split_kind = p_split_kind
          and (p_as_of_date is null or pr.as_of_date = p_as_of_date)
        intersect
        select pr.as_of_date from triage.prediction_ranks pr
        where pr.model_id = p_model_b and pr.split_kind = p_split_kind
          and (p_as_of_date is null or pr.as_of_date = p_as_of_date)
        order by 1
    loop
        select count(*)::int into v_n_a from triage.prediction_ranks pr
         where pr.model_id = p_model_a and pr.split_kind = p_split_kind and pr.as_of_date = d;
        select count(*)::int into v_n_b from triage.prediction_ranks pr
         where pr.model_id = p_model_b and pr.split_kind = p_split_kind and pr.as_of_date = d;

        return query
        with a as (
            select pr.entity_id, pr.rank_abs from triage.prediction_ranks pr
            where pr.model_id = p_model_a and pr.split_kind = p_split_kind
              and pr.as_of_date = d
        ),
        b as (
            select pr.entity_id, pr.rank_abs from triage.prediction_ranks pr
            where pr.model_id = p_model_b and pr.split_kind = p_split_kind
              and pr.as_of_date = d
        ),
        tops as (
            select
                (select count(*) from a join b using (entity_id)
                  where a.rank_abs <= triage.resolve_k(p_parameter, v_n_a)
                    and b.rank_abs <= triage.resolve_k(p_parameter, v_n_b)) as n_int
        )
        select d,
               triage.resolve_k(p_parameter, v_n_a),
               triage.resolve_k(p_parameter, v_n_b),
               tops.n_int,
               case when (triage.resolve_k(p_parameter, v_n_a)
                          + triage.resolve_k(p_parameter, v_n_b) - tops.n_int) > 0
                    then tops.n_int::double precision
                         / (triage.resolve_k(p_parameter, v_n_a)
                            + triage.resolve_k(p_parameter, v_n_b) - tops.n_int)
               end,
               -- Spearman over commonly-scored entities: Pearson on the rank pairs.
               (select corr(a.rank_abs::double precision, b.rank_abs::double precision)
                  from a join b using (entity_id))
        from tops;
    end loop;
end;
$$;
"""

DOWNGRADE_DDL = r"""
drop function if exists triage.list_overlap(bigint, bigint, text, triage.split_kind, date);
alter table triage.models drop column if exists train_duration_ms;
"""


def upgrade():
    op.execute(UPGRADE_DDL)


def downgrade():
    op.execute(DOWNGRADE_DDL)
