"""model fit geometry — the ordered feature list each estimator was actually fitted on (#14)

Revision ID: 0021_model_feature_geometry
Revises: 0020_pinball_metric
Create Date: 2026-09-17

``predict_forward`` scored a model against ``triage.matrices.feature_names`` — the matrix's
*build* order — while the estimator had been fitted on a different order. Nothing raised: the
column *sets* are identical, so ``_design_X``'s missing-column check passes and
``frame.select(feature_names)`` silently transposes the design matrix. Wrong numbers, no error.

No existing column records the fit order for *both* build paths:

* ``triage run`` fits on the feature-group projection, which is **sorted** (``run.py``
  ``_feature_subsets`` sorts even when no ``feature_groups`` block is declared), and
  ``model_groups.feature_list`` happens to hold it.
* ``triage retrain`` builds a full matrix with no projection and fits on the matrix's **build**
  order — then rejoins its group, because ``_model_group_hash`` sorts the list before hashing. Its
  group row therefore holds a *different* order from the one its estimator saw.

Neither ``matrices.feature_names`` nor ``model_groups.feature_list`` is right for both, so a fix
that picked either would have repaired one path and broken the other. This column records the
order per *model*, written from the list ``build_model`` hands to ``fit``.

The backfill tells the two paths apart by ``triage.runs.purpose``, which is exactly the
discriminator (ADR-0018). A model whose run row is gone (``models.run_id`` is
``on delete set null``) cannot be classified and falls back to the model-group list — correct for
every model built by ``triage run``, and wrong only for a retrained model whose run was deleted.
That residue is small and knowable rather than silent::

    select m.model_id from triage.models m
     where m.run_id is null and m.feature_list is distinct from (
             select mx.feature_names from triage.matrices mx
              where mx.matrix_uuid = m.train_matrix_uuid);
"""

from alembic import op

revision = "0021_model_feature_geometry"
down_revision = "0020_pinball_metric"
branch_labels = None
depends_on = None

_COMMENT = (
    "The ordered feature columns this estimator was fitted on — the exact list passed to "
    "frame.select() before fit. Scoring MUST use this order, not matrices.feature_names "
    "(build order) nor model_groups.feature_list (sorted, and wrong for retrained models)."
)

# 'retrain' runs fit on the train matrix's build order; everything else (purpose='experiment',
# and historical rows predating purposes) fits on the sorted projection model_groups records.
_BACKFILL = """
update triage.models m
   set feature_list = coalesce(
         case
           when (select r.purpose from triage.runs r where r.run_id = m.run_id) = 'retrain'
           then (select mx.feature_names from triage.matrices mx
                  where mx.matrix_uuid = m.train_matrix_uuid)
         end,
         (select g.feature_list from triage.model_groups g
           where g.model_group_id = m.model_group_id)
       )
 where m.feature_list is null
"""


def upgrade() -> None:
    op.execute("alter table triage.models add column feature_list text[]")
    op.execute(f"comment on column triage.models.feature_list is {_COMMENT!r}")
    op.execute(_BACKFILL)


def downgrade() -> None:
    op.execute("alter table triage.models drop column feature_list")
