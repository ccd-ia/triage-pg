"""postmodeling diagnostics, part 2 — crosstabs + error-analysis tables (plan P5)

Revision ID: 0017_diagnostics_tables
Revises: 0016_diagnostics
Create Date: 2026-07-06

The ADR-0011 persistence for the two feature-value diagnostics: matrices live in
Parquet (not PG), so `triage postmodel` computes from the matrix ONCE and persists
here; the dashboard and CLI read the tables (no UI-side math, ADR-0012).

* ``triage.crosstabs`` — per (model, split, date, top-k cut, feature, stat): the stat's
  value among the SELECTED (top-k) vs the REST, and their ratio (NULL-guarded).
  stat ∈ {mean, std, nonzero_rate}.
* ``triage.error_analysis`` — the "predict on the errors" diagnostic: a shallow
  interpretable tree is fitted on the model's mistakes (fp: selected & outcome=0,
  within the selected population; fn: passed-over & outcome=1, within the rest) and
  each leaf becomes a human-readable rule with its support and error rate.

Both upsert on their PK — re-running a diagnostic refreshes in place.
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "0017_diagnostics_tables"
down_revision = "0016_diagnostics"
branch_labels = None
depends_on = None


UPGRADE_DDL = r"""
create table triage.crosstabs (
    model_id       bigint not null references triage.models(model_id) on delete cascade,
    split_kind     triage.split_kind not null,
    as_of_date     date   not null,
    parameter      text   not null,               -- the top-k cut, e.g. '100_abs'
    feature        text   not null,
    stat           text   not null check (stat in ('mean', 'std', 'nonzero_rate')),
    selected_value double precision,
    rest_value     double precision,
    ratio          double precision,              -- selected / rest (NULL when rest = 0)
    computed_at    timestamptz not null default now(),
    primary key (model_id, split_kind, as_of_date, parameter, feature, stat)
);

create table triage.error_analysis (
    model_id    bigint not null references triage.models(model_id) on delete cascade,
    split_kind  triage.split_kind not null,
    as_of_date  date   not null,
    parameter   text   not null,
    error_kind  text   not null check (error_kind in ('fp', 'fn')),
    rule_id     integer not null,                 -- leaf index, ordered by error rate
    rule        text   not null,                  -- human-readable path, e.g. "a <= 1.5 AND b > 0"
    n_matched   integer not null,
    n_errors    integer not null,
    error_rate  double precision not null,
    depth       integer not null,
    computed_at timestamptz not null default now(),
    primary key (model_id, split_kind, as_of_date, parameter, error_kind, rule_id)
);
"""

DOWNGRADE_DDL = r"""
drop table if exists triage.error_analysis;
drop table if exists triage.crosstabs;
"""


def upgrade():
    op.execute(UPGRADE_DDL)


def downgrade():
    op.execute(DOWNGRADE_DDL)
