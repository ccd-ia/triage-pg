"""run purpose + prediction_date (retrain/forward-score provenance, ADR-0018)

Revision ID: 0003_run_provenance
Revises: 0002_metric_functions
Create Date: 2026-06-19

Retrain and forward-score runs are first-class operations, not experiments. The
inherited triage persisted a dedicated Retrain/RetrainModel record; greenfield
captures the same provenance on ``triage.runs`` (ADR-0018):

* ``purpose`` discriminates the run kind — 'experiment' (the default, what
  ``run_experiment`` produces), 'retrain', or 'forward_score'.
* ``prediction_date`` is the date a retrain/forward-score run targeted. NULL for
  experiment runs (their dates live in the temporal_config / matrix as_of_dates).

This makes "list retrains for model group X and the dates they served" a direct
query, instead of inverting ``as_of = prediction_date - label_timespan`` out of a
train matrix's config. The artifact DAG still carries the full lineage; this is the
operational convenience layer.

Raw SQL in ``op.execute`` on purpose, mirroring 0001/0002's style.
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "0003_run_provenance"
down_revision = "0002_metric_functions"
branch_labels = None
depends_on = None


UPGRADE_DDL = r"""
alter table triage.runs
    add column purpose text not null default 'experiment'
        check (purpose in ('experiment', 'retrain', 'forward_score')),
    add column prediction_date date;
"""


DOWNGRADE_DDL = r"""
alter table triage.runs
    drop column if exists prediction_date,
    drop column if exists purpose;
"""


def upgrade():
    op.execute(UPGRADE_DDL)


def downgrade():
    op.execute(DOWNGRADE_DDL)
