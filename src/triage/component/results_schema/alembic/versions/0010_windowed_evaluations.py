"""windowed evaluation rollup over the per-as_of_date rows

Revision ID: 0010_windowed_evaluations
Revises: 0009_betas_typevolume
Create Date: 2026-06-28

WS1 makes the orchestrator evaluate a model at EVERY test ``as_of_date`` (one
``triage.evaluations`` row-set per prediction time) instead of collapsing the test
window into a single number at its max date. That per-date breakdown is the source
of truth; this migration adds a convenience rollup *view* on top of it.

``triage.evaluations_windowed`` aggregates the per-``as_of_date`` rows back up per
(model, split, subset, metric, parameter) across the whole test window:

* ``n_as_of_dates`` — how many prediction times the model was evaluated at (i.e. how
  many times it was "used") — the count the original triage couldn't give you.
* ``window_start`` / ``window_end`` — the evaluated date span.
* ``value_mean`` / ``value_min`` / ``value_max`` / ``value_stddev`` — the metric's
  behaviour across the window (stability of precision@k over time, etc.).
* ``num_labeled_total`` / ``num_positive_total`` — summed label support.

It is a plain view (no stored state) so it stays correct as new per-date evaluation
rows are appended.
"""

from alembic import op

# revision identifiers, used by Alembic. (id <= 32 chars: results_schema_versions.version_num)
revision = "0010_windowed_evaluations"
down_revision = "0009_betas_typevolume"
branch_labels = None
depends_on = None


UPGRADE_DDL = r"""
create or replace view triage.evaluations_windowed as
select
    model_id,
    split_kind,
    subset_hash,
    metric,
    parameter,
    count(*)            as n_as_of_dates,
    min(as_of_date)     as window_start,
    max(as_of_date)     as window_end,
    avg(value)          as value_mean,
    min(value)          as value_min,
    max(value)          as value_max,
    stddev_samp(value)  as value_stddev,
    sum(num_labeled)    as num_labeled_total,
    sum(num_positive)   as num_positive_total
from triage.evaluations
group by model_id, split_kind, subset_hash, metric, parameter;

comment on view triage.evaluations_windowed is
    'WS1: rollup of the per-as_of_date triage.evaluations rows across a model''s test '
    'window. n_as_of_dates = number of prediction times the model was evaluated at.';
"""


DOWNGRADE_DDL = r"""
drop view if exists triage.evaluations_windowed;
"""


def upgrade():
    op.execute(UPGRADE_DDL)


def downgrade():
    op.execute(DOWNGRADE_DDL)
